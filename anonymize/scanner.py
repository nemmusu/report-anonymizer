"""Scan a path (file or folder) and dispatch each entry to the right adapter.

The scanner is the single source of truth for the file inventory used by all
downstream stages (rules, detector, applier, verifier).

Production hardening:
  * Symlink cycle detection (st_dev, st_ino set).
  * Optional ``follow_symlinks`` (off by default).
  * ``max_depth`` and ``max_file_size_mb`` limits.
  * Honors ``.anonignore`` (gitignore syntax) and optionally ``.gitignore``.
  * User-supplied ``exclude_paths`` (e.g. from the GUI tree-checkbox view).
"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .format_adapters import FormatAdapter, NullAdapter, TextAdapter, get_adapter


# Files we do not even visit. Output trees, dotfiles, build caches, binaries
# we know cannot contain sensitive text we want to anonymize.
SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".venv",
        "venv",
        "node_modules",
        ".idea",
        ".vscode",
        ".cache",
        ".mypy_cache",
        ".pytest_cache",
        ".tox",
        ".ruff_cache",
        "dist",
        "build",
        ".DS_Store",
        ".anon",
    }
)

# Extensions copied as-is without any text inspection.
COPY_AS_IS_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
        ".tiff",
        ".ico",
        ".svg",
        ".mp3",
        ".wav",
        ".ogg",
        ".mp4",
        ".webm",
        ".avi",
        ".mov",
        ".gguf",
        ".bin",
        ".so",
        ".o",
        ".a",
        ".dll",
        ".dylib",
        ".class",
        ".jar",
        ".war",
        ".apk",
        ".aab",
        ".ipa",
        ".dex",
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".pyc",
        ".pyo",
    }
)

DEFAULT_MAX_FILE_SIZE_MB: int = 50


@dataclass
class ScannedFile:
    """A file the engine knows how to handle (or has decided to copy as-is)."""

    path: Path  # absolute
    rel: Path  # relative to the scan root (or to the parent directory for single files)
    adapter: FormatAdapter
    size: int
    is_text_like: bool  # True if extracted segments are reasonable to feed to the LLM
    skipped: bool = False  # True if we will copy the file as-is (binary)
    skip_reason: str = ""

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class ScanResult:
    """Inventory of a scan run."""

    root: Path
    is_single_file: bool
    files: list[ScannedFile] = field(default_factory=list)
    skipped_dirs: list[tuple[str, str]] = field(default_factory=list)  # (path, reason)

    @property
    def text_like(self) -> Iterator[ScannedFile]:
        return (f for f in self.files if f.is_text_like and not f.skipped)

    def breakdown_by_adapter(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.files:
            key = f.adapter.__class__.__name__ if not f.skipped else "binary"
            out[key] = out.get(key, 0) + 1
        return out

    def breakdown_by_ext(self) -> dict[str, tuple[int, int]]:
        out: dict[str, tuple[int, int]] = {}
        for f in self.files:
            ext = f.path.suffix.lower() or "(no ext)"
            cur_count, cur_size = out.get(ext, (0, 0))
            out[ext] = (cur_count + 1, cur_size + f.size)
        return out


def _should_skip_dir(name: str) -> bool:
    return name in SKIP_DIRS


def _classify(
    path: Path,
    *,
    pdf_strategy: str = "inplace",
    max_file_size_bytes: Optional[int] = None,
) -> tuple[FormatAdapter, bool, bool, str]:
    """Return (adapter, is_text_like, skipped, skip_reason) for a single file."""
    ext = path.suffix.lower()
    try:
        size = path.stat().st_size
    except Exception:
        return NullAdapter(), False, True, "unreadable"
    if max_file_size_bytes and size > max_file_size_bytes:
        return NullAdapter(), False, True, f"too large ({size // (1024 * 1024)} MB)"
    if ext in COPY_AS_IS_EXTENSIONS:
        return NullAdapter(), False, True, f"binary ({ext})"
    adapter = get_adapter(path, pdf_strategy=pdf_strategy)
    if isinstance(adapter, NullAdapter):
        return adapter, False, True, "unsupported / not text"
    is_text_like = True
    if isinstance(adapter, TextAdapter):
        # Heuristic: real binary files contain NUL bytes in the first ~8KB.
        # UTF-16 BOMs are detected and accepted here.
        try:
            head = path.read_bytes()[:8192]
        except Exception:
            return NullAdapter(), False, True, "unreadable"
        if head.startswith((b"\xff\xfe", b"\xfe\xff", b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
            return adapter, True, False, ""
        if b"\x00" in head:
            return NullAdapter(), False, True, "binary content"
    return adapter, is_text_like, False, ""


# ---- ignore patterns ------------------------------------------------------


def _read_ignore_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def _matches_any(path: Path, root: Path, patterns: list[str]) -> bool:
    """Very small subset of gitignore: glob match against the relative path."""
    if not patterns:
        return False
    try:
        rel = path.relative_to(root).as_posix()
    except Exception:
        rel = path.as_posix()
    for pat in patterns:
        # Treat patterns ending in '/' as directory matchers.
        directory_only = pat.endswith("/")
        p = pat.rstrip("/")
        if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(path.name, p):
            return True
        if directory_only and (rel.startswith(p + "/") or rel == p):
            return True
        # Match leading '/'-anchored patterns
        if p.startswith("/") and fnmatch.fnmatch(rel, p[1:]):
            return True
        # Match patterns referring to a path component
        if "/" not in p and any(fnmatch.fnmatch(part, p) for part in rel.split("/")):
            return True
    return False


# ---- main scan ------------------------------------------------------------


def scan_path(
    root: Path,
    *,
    pdf_strategy: str = "inplace",
    extra_skip_dirs: Optional[Iterable[str]] = None,
    follow_symlinks: bool = False,
    max_depth: Optional[int] = None,
    max_file_size_mb: Optional[int] = DEFAULT_MAX_FILE_SIZE_MB,
    exclude_paths: Optional[Iterable[Path]] = None,
    respect_gitignore: bool = True,
    extra_ignore_patterns: Optional[Iterable[str]] = None,
) -> ScanResult:
    """Scan a file or directory.

    For a single file: ``rel`` will be just the filename.
    For a directory: ``rel`` is relative to ``root``.

    The scanner is robust against symlink cycles (it tracks visited
    ``(st_dev, st_ino)`` pairs) and unreadable files (skipped with reason).
    """
    root = root.resolve()
    skip = set(SKIP_DIRS)
    if extra_skip_dirs:
        skip.update(extra_skip_dirs)
    max_size_bytes = (max_file_size_mb or 0) * 1024 * 1024 or None
    excluded = {Path(p).resolve() for p in (exclude_paths or [])}

    if root.is_file():
        adapter, txt, skipped, reason = _classify(
            root, pdf_strategy=pdf_strategy, max_file_size_bytes=max_size_bytes
        )
        size = 0
        try:
            size = root.stat().st_size
        except Exception:
            pass
        return ScanResult(
            root=root.parent,
            is_single_file=True,
            files=[
                ScannedFile(
                    path=root,
                    rel=Path(root.name),
                    adapter=adapter,
                    size=size,
                    is_text_like=txt,
                    skipped=skipped,
                    skip_reason=reason,
                )
            ],
        )

    # Collect ignore patterns
    patterns: list[str] = list(extra_ignore_patterns or [])
    patterns.extend(_read_ignore_file(root / ".anonignore"))
    if respect_gitignore:
        patterns.extend(_read_ignore_file(root / ".gitignore"))

    files: list[ScannedFile] = []
    skipped_dirs: list[tuple[str, str]] = []
    for p in _iter_files(
        root,
        skip,
        follow_symlinks=follow_symlinks,
        max_depth=max_depth,
        excluded=excluded,
        ignore_patterns=patterns,
        skipped_dirs_collector=skipped_dirs,
    ):
        try:
            adapter, txt, skipped, reason = _classify(
                p,
                pdf_strategy=pdf_strategy,
                max_file_size_bytes=max_size_bytes,
            )
            sz = p.stat().st_size
            rel = p.relative_to(root)
        except Exception:
            continue
        files.append(
            ScannedFile(
                path=p,
                rel=rel,
                adapter=adapter,
                size=sz,
                is_text_like=txt,
                skipped=skipped,
                skip_reason=reason,
            )
        )
    files.sort(key=lambda f: str(f.rel))
    return ScanResult(
        root=root,
        is_single_file=False,
        files=files,
        skipped_dirs=skipped_dirs,
    )


def scan_files(
    paths: Iterable[Path],
    *,
    pdf_strategy: str = "inplace",
    max_file_size_mb: Optional[int] = DEFAULT_MAX_FILE_SIZE_MB,
) -> ScanResult:
    """Scan an explicit list of file paths (multi-file mode).

    The ``rel`` is set to ``path.name``; the consumer (applier) writes the
    output as ``<output_dir>/<basename>``.
    """
    out: list[ScannedFile] = []
    max_size_bytes = (max_file_size_mb or 0) * 1024 * 1024 or None
    for p in paths:
        p = Path(p).resolve()
        if not p.is_file():
            continue
        adapter, txt, skipped, reason = _classify(
            p, pdf_strategy=pdf_strategy, max_file_size_bytes=max_size_bytes
        )
        try:
            size = p.stat().st_size
        except Exception:
            size = 0
        out.append(
            ScannedFile(
                path=p,
                rel=Path(p.name),
                adapter=adapter,
                size=size,
                is_text_like=txt,
                skipped=skipped,
                skip_reason=reason,
            )
        )
    out.sort(key=lambda f: str(f.rel))
    common = Path(".")
    return ScanResult(root=common, is_single_file=False, files=out)


def _iter_files(
    root: Path,
    skip: set[str],
    *,
    follow_symlinks: bool,
    max_depth: Optional[int],
    excluded: set[Path],
    ignore_patterns: list[str],
    skipped_dirs_collector: list[tuple[str, str]],
    visited: Optional[set[tuple[int, int]]] = None,
    depth: int = 0,
) -> Iterator[Path]:
    if visited is None:
        visited = set()
    try:
        st = root.stat()
        key = (st.st_dev, st.st_ino)
        if key in visited:
            skipped_dirs_collector.append((str(root), "cycle"))
            return
        visited.add(key)
    except Exception:
        return
    if max_depth is not None and depth > max_depth:
        skipped_dirs_collector.append((str(root), f"max_depth ({depth})"))
        return
    try:
        entries = sorted(root.iterdir())
    except Exception:
        return
    for entry in entries:
        try:
            if entry.is_symlink() and not follow_symlinks:
                continue
            if entry.resolve() in excluded:
                continue
            if entry.is_dir():
                if entry.name in skip:
                    skipped_dirs_collector.append((str(entry), "noise dir"))
                    continue
                if _matches_any(entry, root, ignore_patterns):
                    skipped_dirs_collector.append((str(entry), "ignore match"))
                    continue
                yield from _iter_files(
                    entry,
                    skip,
                    follow_symlinks=follow_symlinks,
                    max_depth=max_depth,
                    excluded=excluded,
                    ignore_patterns=ignore_patterns,
                    skipped_dirs_collector=skipped_dirs_collector,
                    visited=visited,
                    depth=depth + 1,
                )
            else:
                if _matches_any(entry, root, ignore_patterns):
                    continue
                yield entry
        except Exception:
            continue


__all__ = [
    "ScannedFile",
    "ScanResult",
    "scan_path",
    "scan_files",
    "SKIP_DIRS",
    "COPY_AS_IS_EXTENSIONS",
    "DEFAULT_MAX_FILE_SIZE_MB",
]
