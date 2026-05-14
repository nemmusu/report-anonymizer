"""Markdown-aware variant of :func:`anonymize.chunker.chunk_text`.

The flat chunker only knows about whitespace separators, so a long
document can have its tables / code fences split mid-row, costing
the LLM the disambiguation context (header rows, section title).
This splitter walks the text once, recognises common structural
blocks (heading, fenced code, Markdown table, list, blockquote,
hr, paragraph) and packs them greedily into chunks. It never
splits inside a structural block, the only way a chunk exceeds
the soft cap is when a single block does.

For any block bigger than the hard cap we fall back to the flat
``_find_break`` so we still respect the absolute upper limit.

The structured chunks also carry a ``section`` attribute holding
the nearest preceding heading; the detector prompt injects it so
``Username: admin`` under ``## Production credentials`` always
travels with that context.
"""
from __future__ import annotations

import re
from typing import Iterable, Iterator

from .chunker import Chunk, HugeTextStrategy, _find_break, chunk_text
from .format_adapters.base import Segment


_HEADING_RE = re.compile(r"^[ \t]{0,3}(#{1,6})\s+\S.*$", re.MULTILINE)
_HR_RE = re.compile(r"^[ \t]{0,3}([-*_])\1{2,}[ \t]*$")
_LIST_RE = re.compile(r"^[ \t]{0,3}(?:[-*+]\s+|\d+[.)]\s+)")
_BLOCKQUOTE_RE = re.compile(r"^[ \t]{0,3}>")
_FENCE_RE = re.compile(r"^[ \t]{0,3}(```+|~~~+)")
_TABLE_SEP_RE = re.compile(r"^[ \t]*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")


def _is_table_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.count("|") >= 2


def _split_into_blocks(text: str) -> list[tuple[str, str]]:
    """Walk ``text`` line-by-line and return a list of
    ``(kind, body)`` pairs preserving ordering and line endings.

    ``body`` always ends with a newline so concatenating every body
    reproduces the original text exactly.
    """
    if not text:
        return []
    # Make sure we always work with a trailing newline so the
    # last block isn't a special case.
    pad = "" if text.endswith("\n") else "\n"
    lines = (text + pad).splitlines(keepends=True)
    n = len(lines)
    out: list[tuple[str, str]] = []
    i = 0
    while i < n:
        line = lines[i]
        stripped = line.rstrip("\n")
        if not stripped.strip():
            # Blank line: emit a "blank" block of consecutive blanks.
            j = i
            while j < n and not lines[j].strip():
                j += 1
            out.append(("blank", "".join(lines[i:j])))
            i = j
            continue
        # Fenced code block: from the opener to its matching closer.
        m = _FENCE_RE.match(stripped)
        if m:
            fence = m.group(1)
            j = i + 1
            while j < n:
                inner = lines[j].rstrip("\n")
                if inner.strip().startswith(fence):
                    j += 1
                    break
                j += 1
            out.append(("code_fence", "".join(lines[i:j])))
            i = j
            continue
        # Heading.
        if _HEADING_RE.match(stripped):
            out.append(("heading", line))
            i += 1
            continue
        # Horizontal rule.
        if _HR_RE.match(stripped):
            out.append(("hr", line))
            i += 1
            continue
        # Markdown table: a row that LOOKS like a table row, optionally
        # followed by a separator row. We only treat it as a table when
        # we find at least one separator; otherwise it's just a
        # paragraph that contains pipes.
        if _is_table_row(stripped):
            j = i + 1
            has_sep = False
            while j < n:
                inner = lines[j].rstrip("\n")
                if not inner.strip():
                    break
                if _TABLE_SEP_RE.match(inner):
                    has_sep = True
                    j += 1
                    continue
                if _is_table_row(inner):
                    j += 1
                    continue
                break
            if has_sep:
                out.append(("table", "".join(lines[i:j])))
                i = j
                continue
            # No separator → fall through to paragraph handling.
        # Blockquote.
        if _BLOCKQUOTE_RE.match(stripped):
            j = i
            while j < n and lines[j].strip() and _BLOCKQUOTE_RE.match(
                lines[j].rstrip("\n")
            ):
                j += 1
            out.append(("blockquote", "".join(lines[i:j])))
            i = j
            continue
        # List item(s).
        if _LIST_RE.match(stripped):
            j = i
            while j < n and lines[j].strip() and (
                _LIST_RE.match(lines[j].rstrip("\n"))
                or lines[j].startswith((" ", "\t"))
            ):
                j += 1
            out.append(("list", "".join(lines[i:j])))
            i = j
            continue
        # Plain paragraph: gobble until blank line.
        j = i
        while j < n and lines[j].strip() and not (
            _HEADING_RE.match(lines[j].rstrip("\n"))
            or _FENCE_RE.match(lines[j].rstrip("\n"))
            or _HR_RE.match(lines[j].rstrip("\n"))
        ):
            j += 1
        out.append(("paragraph", "".join(lines[i:j])))
        i = j
    return out


def _strip_leading_hashes(heading: str) -> str:
    return heading.lstrip("# \t").rstrip("\n").strip()


def _force_split_block(
    seg_id: str,
    file_rel: str,
    body: str,
    *,
    start: int,
    chunk_id_start: int,
    section: str,
    max_chars: int,
    overlap: int,
) -> tuple[list[Chunk], int]:
    """Fall back to the flat splitter when a single structural block
    exceeds the hard cap. Reuses ``_find_break`` to honour the same
    soft-boundary preferences. Returns ``(chunks, next_chunk_id)``."""
    if len(body) <= max_chars:
        return (
            [
                Chunk(
                    seg_id=seg_id,
                    chunk_id=chunk_id_start,
                    start=start,
                    text=body,
                    file_rel=file_rel,
                    section=section,
                )
            ],
            chunk_id_start + 1,
        )
    out: list[Chunk] = []
    pos = 0
    cid = chunk_id_start
    while pos < len(body):
        ideal_end = pos + max_chars
        if ideal_end >= len(body):
            out.append(
                Chunk(
                    seg_id=seg_id,
                    chunk_id=cid,
                    start=start + pos,
                    text=body[pos:],
                    file_rel=file_rel,
                    section=section,
                )
            )
            break
        target = pos + (max_chars - overlap)
        end = _find_break(body, target, max_chars=ideal_end)
        if end <= pos:
            end = ideal_end
        out.append(
            Chunk(
                seg_id=seg_id,
                chunk_id=cid,
                start=start + pos,
                text=body[pos:end],
                file_rel=file_rel,
                section=section,
            )
        )
        cid += 1
        pos = max(end - overlap, pos + 1)
    return out, cid


def chunk_text_structured(
    seg_id: str,
    text: str,
    *,
    file_rel: str,
    max_chars: int = 5000,
    overlap: int = 200,
    huge_threshold: int = 5_000_000,
    huge_strategy: HugeTextStrategy = HugeTextStrategy.PROCESS_FULL,
) -> list[Chunk]:
    """Markdown-structure-aware splitter.

    Behaves like :func:`chunker.chunk_text` for the boring cases
    (small text, missing structure) and packs structural blocks
    atomically when they exist (tables, code fences, headings).
    """
    if not text:
        return []
    if len(text) > huge_threshold and huge_strategy == HugeTextStrategy.SKIP:
        return []
    if len(text) > huge_threshold and huge_strategy == HugeTextStrategy.TRUNCATE:
        text = text[:huge_threshold]
    if len(text) <= max_chars:
        return [
            Chunk(
                seg_id=seg_id,
                chunk_id=0,
                start=0,
                text=text,
                file_rel=file_rel,
            )
        ]

    blocks = _split_into_blocks(text)
    if not blocks:
        return [
            Chunk(seg_id=seg_id, chunk_id=0, start=0, text=text, file_rel=file_rel)
        ]

    # Pre-compute the absolute character offset of each block.
    offsets: list[int] = []
    cursor = 0
    for _, body in blocks:
        offsets.append(cursor)
        cursor += len(body)

    chunks: list[Chunk] = []
    cid = 0
    current_section = ""
    i = 0
    n = len(blocks)
    while i < n:
        # Skip leading blank blocks (they'd produce empty chunks).
        while i < n and blocks[i][0] == "blank":
            i += 1
        if i >= n:
            break

        chunk_start_block = i
        chunk_start_offset = offsets[i]
        section_for_chunk = current_section
        # If the chunk opens with a heading, it doesn't need an
        # injected section header, the chunk text already contains it.
        starts_with_heading = blocks[i][0] == "heading"
        if starts_with_heading:
            section_for_chunk = ""
            current_section = _strip_leading_hashes(blocks[i][1])

        # Greedy pack consecutive blocks until the next one would
        # push us past max_chars.
        chunk_size = 0
        j = i
        while j < n:
            kind, body = blocks[j]
            blen = len(body)
            # Stop BEFORE a heading: the next chunk opens with it.
            # Exception: if the current chunk has only packed
            # heading(s) + blank lines, keep going so a chunk doesn't
            # end up containing nothing but a lonely heading.
            packed_so_far = blocks[chunk_start_block:j]
            packed_kinds = {b[0] for b in packed_so_far}
            has_real_body = bool(packed_kinds - {"heading", "blank"})
            if (
                kind == "heading"
                and j != chunk_start_block
                and has_real_body
            ):
                break
            if chunk_size + blen > max_chars and j > chunk_start_block:
                # Would overflow. Two cases:
                #  - We already have a real body block packed → break
                #    and emit the current chunk; the overflowing block
                #    starts the next chunk.
                #  - We only have heading + blank so far → keep going,
                #    we MUST attach a body block to the heading even if
                #    it overflows the soft cap (force_split / atomic
                #    handling later will decide what to do with the
                #    oversized result).
                if has_real_body:
                    break
            chunk_size += blen
            j += 1

        # Concatenate the packed blocks. If any single block is larger
        # than max_chars we'll have packed only it (j == chunk_start_block + 1)
        # and chunk_size > max_chars; we either keep it atomic (table /
        # code fence, splitting them defeats the purpose of the
        # structured splitter) or fall back to flat word-split for the
        # remaining cases.
        packed = blocks[chunk_start_block:j]
        body = "".join(b[1] for b in packed)

        # Update the running section pointer based on the headings we
        # just packed (the LAST heading wins for the next chunk).
        for kind, txt in packed:
            if kind == "heading":
                current_section = _strip_leading_hashes(txt)

        # If the packed set contains any atomic structural block
        # (table / code_fence) we do NOT slice the resulting body -
        # even if it overflows max_chars. Splitting a table or code
        # fence is exactly what the structured chunker exists to
        # avoid; an oversized chunk is the lesser evil here.
        atomic_kinds = {"table", "code_fence"}
        is_atomic = any(k in atomic_kinds for k, _ in packed)

        if len(body) > max_chars and not is_atomic:
            sub_chunks, cid = _force_split_block(
                seg_id,
                file_rel,
                body,
                start=chunk_start_offset,
                chunk_id_start=cid,
                section=section_for_chunk,
                max_chars=max_chars,
                overlap=overlap,
            )
            chunks.extend(sub_chunks)
        else:
            chunks.append(
                Chunk(
                    seg_id=seg_id,
                    chunk_id=cid,
                    start=chunk_start_offset,
                    text=body,
                    file_rel=file_rel,
                    section=section_for_chunk,
                )
            )
            cid += 1
        i = j
    return chunks


def chunk_segments_structured(
    segments: Iterable[Segment],
    *,
    file_rel: str,
    max_chars: int = 5000,
    overlap: int = 200,
    huge_threshold: int = 5_000_000,
    huge_strategy: HugeTextStrategy = HugeTextStrategy.PROCESS_FULL,
) -> Iterator[Chunk]:
    for seg in segments:
        for c in chunk_text_structured(
            seg.seg_id,
            seg.text,
            file_rel=file_rel,
            max_chars=max_chars,
            overlap=overlap,
            huge_threshold=huge_threshold,
            huge_strategy=huge_strategy,
        ):
            yield c


__all__ = [
    "chunk_text_structured",
    "chunk_segments_structured",
]
