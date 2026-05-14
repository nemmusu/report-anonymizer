"""Scanner edge cases: symlink cycles, max_depth, gitignore."""
from __future__ import annotations

import os
from pathlib import Path

from anonymize.scanner import scan_path


def test_symlink_cycle_safe(tmp_path: Path) -> None:
    a = tmp_path / "a"
    a.mkdir()
    (a / "file.md").write_text("hello", encoding="utf-8")
    try:
        os.symlink(tmp_path, a / "loop")
    except OSError:
        return  # platform without symlink support
    res = scan_path(tmp_path, follow_symlinks=True, max_depth=5)
    assert res.files  # at least one


def test_max_depth_caps(tmp_path: Path) -> None:
    deep = tmp_path
    for i in range(6):
        deep = deep / f"d{i}"
    deep.mkdir(parents=True)
    (deep / "x.md").write_text("hi", encoding="utf-8")
    res = scan_path(tmp_path, max_depth=2)
    rels = [str(f.rel) for f in res.files]
    assert all("d3" not in r for r in rels)


def test_gitignore_honored(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("ignored.md\n", encoding="utf-8")
    (tmp_path / "ignored.md").write_text("x", encoding="utf-8")
    (tmp_path / "kept.md").write_text("y", encoding="utf-8")
    res = scan_path(tmp_path, respect_gitignore=True)
    rels = {str(f.rel) for f in res.files}
    assert "kept.md" in rels and "ignored.md" not in rels


def test_anonignore_supersedes(tmp_path: Path) -> None:
    (tmp_path / ".anonignore").write_text("*.bak\n", encoding="utf-8")
    (tmp_path / "x.md").write_text("ok", encoding="utf-8")
    (tmp_path / "x.bak").write_text("nope", encoding="utf-8")
    res = scan_path(tmp_path)
    rels = {str(f.rel) for f in res.files}
    assert "x.md" in rels and "x.bak" not in rels


def test_max_file_size_skips_large(tmp_path: Path) -> None:
    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * (2 * 1024 * 1024))
    res = scan_path(tmp_path, max_file_size_mb=1)
    matches = [f for f in res.files if str(f.rel) == "big.txt"]
    assert matches and matches[0].skipped
