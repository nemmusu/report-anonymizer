"""Split long segment text into LLM-friendly chunks.

The chunker operates on segments produced by ``FormatAdapter.extract``. It
guarantees that each chunk is ``<= max_chars`` characters and tries to break
on paragraph / line / sentence boundaries when possible to avoid splitting
identifiers in half.

Production hardening:
  * ``HugeTextStrategy`` for segments far above ``max_chars`` -
    ``skip`` (default-safe), ``truncate`` (first N chars), ``process_full``.
  * No-natural-boundary chunks are still emitted (force-split) so we never
    lose content.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Iterator

from .format_adapters.base import Segment


@dataclass
class Chunk:
    seg_id: str
    chunk_id: int  # 0-based within the segment
    start: int  # offset into the segment text
    text: str
    file_rel: str  # path relative to the scan root, for telemetry
    truncated: bool = False  # True if the strategy dropped the tail
    # Nearest preceding heading (Markdown-style) when the structured
    # chunker recognises one. Empty for the flat chunker / for chunks
    # that already start with their own heading.
    section: str = ""

    @property
    def end(self) -> int:
        return self.start + len(self.text)


class HugeTextStrategy(str, Enum):
    SKIP = "skip"
    TRUNCATE = "truncate"
    PROCESS_FULL = "process_full"


_BOUNDARIES = ("\n\n", "\n", ". ", "; ", ", ", " ")


def _find_break(text: str, target: int, max_chars: int) -> int:
    """Find a split point near ``target`` that prefers paragraph/line/sentence
    boundaries, but never goes past ``max_chars``."""
    upper = min(len(text), max_chars)
    if upper <= target:
        return upper
    for sep in _BOUNDARIES:
        idx = text.rfind(sep, target, upper)
        if idx != -1:
            return idx + len(sep)
    return upper


def chunk_text(
    seg_id: str,
    text: str,
    *,
    file_rel: str,
    max_chars: int = 10000,
    overlap: int = 200,
    huge_threshold: int = 5_000_000,
    huge_strategy: HugeTextStrategy = HugeTextStrategy.PROCESS_FULL,
) -> list[Chunk]:
    """Slice ``text`` into chunks of at most ``max_chars`` characters."""
    if not text:
        return []
    if len(text) > huge_threshold and huge_strategy == HugeTextStrategy.SKIP:
        return []
    if len(text) > huge_threshold and huge_strategy == HugeTextStrategy.TRUNCATE:
        text = text[:huge_threshold]
    if len(text) <= max_chars:
        return [Chunk(seg_id=seg_id, chunk_id=0, start=0, text=text, file_rel=file_rel)]
    chunks: list[Chunk] = []
    pos = 0
    cid = 0
    while pos < len(text):
        ideal_end = pos + max_chars
        if ideal_end >= len(text):
            chunks.append(
                Chunk(
                    seg_id=seg_id,
                    chunk_id=cid,
                    start=pos,
                    text=text[pos:],
                    file_rel=file_rel,
                )
            )
            break
        target = pos + (max_chars - overlap)
        end = _find_break(text, target, max_chars=ideal_end)
        if end <= pos:
            end = ideal_end
        chunks.append(
            Chunk(
                seg_id=seg_id,
                chunk_id=cid,
                start=pos,
                text=text[pos:end],
                file_rel=file_rel,
            )
        )
        cid += 1
        pos = max(end - overlap, pos + 1)
    return chunks


def chunk_segments(
    segments: Iterable[Segment],
    *,
    file_rel: str,
    max_chars: int = 10000,
    overlap: int = 200,
    huge_threshold: int = 5_000_000,
    huge_strategy: HugeTextStrategy = HugeTextStrategy.PROCESS_FULL,
) -> Iterator[Chunk]:
    for seg in segments:
        for c in chunk_text(
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
    "Chunk",
    "HugeTextStrategy",
    "chunk_text",
    "chunk_segments",
]
