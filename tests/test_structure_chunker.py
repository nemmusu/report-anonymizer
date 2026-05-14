"""Structure-aware chunker, preserves table / code / heading
boundaries and injects the parent heading as ``Chunk.section``."""
from __future__ import annotations

from anonymize.structure_chunker import (
    _split_into_blocks,
    chunk_text_structured,
)


def _kinds(blocks):
    return [k for k, _ in blocks]


def test_split_blocks_recognises_each_kind():
    text = (
        "# Title\n\n"
        "Intro paragraph.\n\n"
        "| col | val |\n|---|---|\n| a | 1 |\n| b | 2 |\n\n"
        "```python\nprint('x')\n```\n\n"
        "- item 1\n- item 2\n\n"
        "> quoted line\n\n"
        "---\n\n"
        "Trailing paragraph.\n"
    )
    kinds = _kinds(_split_into_blocks(text))
    # Order matters: heading, blank, paragraph, blank, table, blank,
    # code_fence, blank, list, blank, blockquote, blank, hr, blank,
    # paragraph
    assert "heading" in kinds
    assert "table" in kinds
    assert "code_fence" in kinds
    assert "list" in kinds
    assert "blockquote" in kinds
    assert "hr" in kinds
    assert "paragraph" in kinds


def test_table_kept_atomic():
    """A table that fits in one chunk must arrive intact, even when
    surrounding content forces a split before/after it."""
    table = (
        "| user      | password    |\n"
        "|-----------|-------------|\n"
        "| j.doe     | Welcome01!  |\n"
        "| svc-bk    | Hunter2!    |\n"
    )
    text = (
        "# Section A\n\n"
        + ("Lorem ipsum dolor sit amet. " * 200)
        + "\n\n"
        + "# Credentials\n\n"
        + table
        + "\n\n"
        + ("More prose. " * 200)
    )
    chunks = chunk_text_structured(
        seg_id="0", text=text, file_rel="x.md", max_chars=500
    )
    # Find the chunk that contains the table header row.
    matching = [
        c for c in chunks if "| user      | password    |" in c.text
    ]
    assert len(matching) == 1, (
        f"table header should land in exactly one chunk, found "
        f"{len(matching)}"
    )
    assert "Welcome01!" in matching[0].text
    assert "Hunter2!" in matching[0].text
    assert "|---" in matching[0].text


def test_chunk_carries_parent_heading_as_section():
    text = (
        "# Production credentials\n\n"
        + ("Some prose introducing the topic. " * 100)
        + "\n\n"
        + ("More prose that lives under the same heading. " * 100)
    )
    chunks = chunk_text_structured(
        seg_id="0", text=text, file_rel="x.md", max_chars=400
    )
    # First chunk opens with the heading itself; section attribute is
    # empty (no need to inject what's already in the body).
    assert chunks[0].text.startswith("# Production credentials")
    assert chunks[0].section == ""
    # Subsequent chunks must remember the heading.
    assert any(c.section == "Production credentials" for c in chunks[1:])


def test_code_fence_not_split_in_half():
    fence = "```python\n" + "x = 1\n" * 60 + "```\n"
    text = "# Title\n\n" + fence + "\nepilogue.\n"
    chunks = chunk_text_structured(
        seg_id="0", text=text, file_rel="x.md", max_chars=300
    )
    # The code fence is bigger than max_chars; it should appear in a
    # single chunk (force-split fallback only kicks in if ONE block
    # exceeds the cap; we accept that as a single oversized chunk
    # rather than slicing it). Verify the opener and closer land in
    # the SAME chunk.
    matching = [c for c in chunks if "```python" in c.text]
    assert len(matching) == 1
    assert matching[0].text.count("```") == 2


def test_no_chunk_ends_with_a_lonely_heading():
    text = (
        ("Pre-heading paragraph. " * 30) + "\n\n"
        "## A new section\n\n"
        + ("Body content. " * 30)
    )
    chunks = chunk_text_structured(
        seg_id="0", text=text, file_rel="x.md", max_chars=400
    )
    for c in chunks:
        # Last non-blank line of every chunk must not be a heading.
        last = [ln for ln in c.text.splitlines() if ln.strip()]
        if not last:
            continue
        assert not last[-1].lstrip().startswith("#"), (
            f"chunk ends with a heading line, should ride with body: "
            f"{c.text[-200:]!r}"
        )


def test_short_text_emits_single_chunk():
    text = "Just one short paragraph, no structure.\n"
    chunks = chunk_text_structured(
        seg_id="0", text=text, file_rel="x.md", max_chars=5000
    )
    assert len(chunks) == 1
    assert chunks[0].text == text
    assert chunks[0].section == ""


def test_huge_single_block_falls_back_to_word_split():
    """A single paragraph bigger than ``max_chars`` falls through to
    the flat splitter so we still respect the hard cap."""
    text = "word " * 4000  # ~20k chars, no structure
    chunks = chunk_text_structured(
        seg_id="0", text=text, file_rel="x.md", max_chars=1000
    )
    assert len(chunks) > 1
    assert all(len(c.text) <= 1000 + 200 for c in chunks), (
        "no chunk should overflow max_chars + overlap by more than a "
        "small margin"
    )
