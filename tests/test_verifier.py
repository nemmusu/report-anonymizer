"""Verifier hardening: entity decode, NFKC, zero-width strip."""
from __future__ import annotations

from pathlib import Path

import yaml

from anonymize.verifier import verify, write_verifier_report


PATTERNS = """\
auto_promote:
  - name: ipv4
    regex: '\\b(?:25[0-5]|2[0-4]\\d|[01]?\\d?\\d)(?:\\.(?:25[0-5]|2[0-4]\\d|[01]?\\d?\\d)){3}\\b'
    allow: []
"""


def _setup(tmp_path: Path) -> Path:
    pat = tmp_path / "patterns.yml"
    pat.write_text(PATTERNS, encoding="utf-8")
    return pat


def test_verifier_finds_plain_ip(tmp_path: Path) -> None:
    pat = _setup(tmp_path)
    out = tmp_path / "out.txt"
    out.write_text("server is 10.20.30.40\n", encoding="utf-8")
    rep = verify(tmp_path, patterns_path=pat)
    hits = [h for h in rep.hits if "10.20.30.40" in h.match]
    assert hits


def test_verifier_decodes_html_entities(tmp_path: Path) -> None:
    pat = _setup(tmp_path)
    (tmp_path / "out.txt").write_text(
        "server is 10&#46;20&#46;30&#46;40\n", encoding="utf-8"
    )
    rep = verify(tmp_path, patterns_path=pat)
    assert any("10.20.30.40" in h.match for h in rep.hits)


def test_verifier_strips_zero_width(tmp_path: Path) -> None:
    pat = _setup(tmp_path)
    # Zero-width space inserted between octets
    leak = "10.20\u200b.30.40"
    (tmp_path / "out.txt").write_text(leak, encoding="utf-8")
    rep = verify(tmp_path, patterns_path=pat)
    assert any("10.20.30.40" in h.match for h in rep.hits)


def test_verifier_clean_when_no_leak(tmp_path: Path) -> None:
    pat = _setup(tmp_path)
    (tmp_path / "out.txt").write_text("nothing to see here\n", encoding="utf-8")
    rep = verify(tmp_path, patterns_path=pat)
    assert rep.is_clean
