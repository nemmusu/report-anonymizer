"""Performance smoke tests (warn-only by default)."""
from __future__ import annotations

import time

import pytest

from anonymize.chunker import chunk_text
from anonymize.format_adapters.base import SubstitutionRule, apply_to_text


@pytest.mark.perf
def test_chunker_throughput() -> None:
    text = ("Acme inc. " * 200000)
    t0 = time.monotonic()
    chunks = chunk_text("seg", text, file_rel="x", max_chars=10000)
    elapsed = time.monotonic() - t0
    if elapsed > 5.0:
        pytest.warns(UserWarning, match="slow")
    assert chunks


@pytest.mark.perf
def test_apply_throughput() -> None:
    text = ("hello Acme world. " * 50000)
    rules = [SubstitutionRule(from_=f"Acme", to="V", category="brand")]
    t0 = time.monotonic()
    apply_to_text(text, rules)
    elapsed = time.monotonic() - t0
    assert elapsed < 30.0
