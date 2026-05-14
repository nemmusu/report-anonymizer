"""Unit tests for anonymize.chunker."""
from __future__ import annotations

from anonymize.chunker import Chunk, HugeTextStrategy, chunk_text


def test_chunk_short_text_single_chunk() -> None:
    out = chunk_text("seg1", "hello world", file_rel="x.md", max_chars=50)
    assert len(out) == 1
    assert out[0].text == "hello world"
    assert out[0].chunk_id == 0


def test_chunk_breaks_on_paragraph() -> None:
    text = "para one.\n\npara two body that is reasonably long.\n\nthird para."
    chunks = chunk_text("seg1", text, file_rel="x.md", max_chars=20, overlap=0)
    assert len(chunks) >= 2
    joined = "".join(c.text for c in chunks)
    # round trip preserves content
    assert "para one" in joined and "third para" in joined


def test_huge_text_skip_strategy() -> None:
    big = "a" * 6_000_000
    chunks = chunk_text(
        "seg1", big, file_rel="big.txt", max_chars=10000,
        huge_threshold=5_000_000, huge_strategy=HugeTextStrategy.SKIP,
    )
    assert chunks == []


def test_huge_text_truncate_strategy() -> None:
    big = "x" * 6_000_000
    chunks = chunk_text(
        "seg1", big, file_rel="big.txt", max_chars=10000,
        huge_threshold=5_000_000, huge_strategy=HugeTextStrategy.TRUNCATE,
    )
    total_len = sum(len(c.text) for c in chunks)
    # account for chunk overlap, must fit truncation budget + small slack
    assert total_len <= 5_000_000 + 200_000


def test_huge_text_process_full_strategy() -> None:
    big = "y" * 6_000_000
    chunks = chunk_text(
        "seg1", big, file_rel="big.txt", max_chars=10000,
        huge_threshold=5_000_000, huge_strategy=HugeTextStrategy.PROCESS_FULL,
    )
    assert sum(len(c.text) for c in chunks) >= 5_000_000


def test_chunk_overlap_continuity() -> None:
    text = "abcdefg " * 5000
    chunks = chunk_text("seg", text, file_rel="x.txt", max_chars=2000, overlap=200)
    assert len(chunks) >= 2
    # chunk_id strictly monotonic
    ids = [c.chunk_id for c in chunks]
    assert ids == sorted(ids) and ids == list(range(len(ids)))
