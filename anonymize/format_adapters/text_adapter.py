"""Adapter for plain-text formats (markdown, html, json, yaml, csv, code).

A single segment with the entire file content; substitutions are applied
character-wise via :func:`apply_to_text`.
"""
from __future__ import annotations

from pathlib import Path

from .base import FormatAdapter, Segment, SubstitutionRule, WriteReport, apply_to_text


class TextAdapter(FormatAdapter):
    name = "text"
    EXTENSIONS: frozenset[str] = frozenset(
        {
            ".md",
            ".markdown",
            ".rst",
            ".txt",
            ".html",
            ".htm",
            ".xml",
            ".json",
            ".jsonl",
            ".ndjson",
            ".yml",
            ".yaml",
            ".toml",
            ".ini",
            ".cfg",
            ".conf",
            ".css",
            ".scss",
            ".less",
            ".csv",
            ".tsv",
            ".tex",
            ".sql",
            ".py",
            ".js",
            ".jsx",
            ".ts",
            ".tsx",
            ".sh",
            ".bash",
            ".zsh",
            ".fish",
            ".rb",
            ".pl",
            ".php",
            ".java",
            ".kt",
            ".kts",
            ".groovy",
            ".gradle",
            ".swift",
            ".m",
            ".mm",
            ".c",
            ".cc",
            ".cpp",
            ".cxx",
            ".h",
            ".hh",
            ".hpp",
            ".hxx",
            ".rs",
            ".go",
            ".scala",
            ".clj",
            ".dart",
            ".lua",
            ".r",
            ".jl",
            ".vue",
            ".svelte",
            ".astro",
            ".env",
            ".dockerfile",
            ".makefile",
            ".diff",
            ".patch",
            ".log",
        }
    )
    MIMES: frozenset[str] = frozenset(
        {
            "application/json",
            "application/xml",
            "application/x-yaml",
            "application/yaml",
            "application/javascript",
        }
    )

    extensions = set(EXTENSIONS)
    mimes = set(MIMES)

    def extract(self, path: Path) -> list[Segment]:
        text = self._read(path)
        return [Segment(seg_id="0", text=text, meta={"path": str(path)})]

    def write(
        self,
        src_path: Path,
        dst_path: Path,
        substitutions: list[SubstitutionRule],
    ) -> WriteReport:
        text = self._read(src_path)
        new_text, events = apply_to_text(text, substitutions, seg_id="0")
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        # Preserve the original encoding/newlines minimally: we re-read raw and
        # only diff the textual portion. Newlines inside ``from``/``to`` are
        # author's responsibility.
        dst_path.write_text(new_text, encoding="utf-8")
        return WriteReport(file_rel=str(dst_path), events=events)

    @staticmethod
    def _read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return path.read_text(encoding="utf-8", errors="replace")


__all__ = ["TextAdapter"]
