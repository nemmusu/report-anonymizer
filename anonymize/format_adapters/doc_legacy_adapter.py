"""Adapter for legacy ``.doc`` (binary Word).

Strategy: pre-step ``libreoffice --headless --convert-to docx`` into a temp
directory, then delegate everything to :class:`DocxAdapter`. The output
written by :meth:`write` is a ``.docx`` even when the source was ``.doc``
(this is marked ``is_lossy=True``; the GUI displays a banner).
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .base import FormatAdapter, Segment, SubstitutionRule, WriteReport
from .docx_adapter import DocxAdapter


class DocLegacyAdapter(FormatAdapter):
    name = "doc_legacy"
    extensions = {".doc"}
    mimes = {"application/msword"}
    is_lossy = True

    def __init__(self) -> None:
        self._docx = DocxAdapter()
        if shutil.which("libreoffice") is None and shutil.which("soffice") is None:
            raise RuntimeError(
                "libreoffice (or soffice) is required to handle legacy .doc "
                "files. Install libreoffice and try again."
            )

    @staticmethod
    def _libreoffice_bin() -> str:
        return shutil.which("libreoffice") or shutil.which("soffice") or "libreoffice"

    def _convert_to_docx(self, src: Path, out_dir: Path) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            self._libreoffice_bin(),
            "--headless",
            "--convert-to",
            "docx",
            "--outdir",
            str(out_dir),
            str(src),
        ]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"libreoffice conversion failed for {src}: {proc.stderr.strip()[:300]}"
            )
        out_path = out_dir / (src.stem + ".docx")
        if not out_path.exists():
            cands = list(out_dir.glob(src.stem + "*.docx"))
            if cands:
                out_path = cands[0]
        if not out_path.exists():
            raise RuntimeError(
                f"libreoffice did not produce a .docx for {src} in {out_dir}"
            )
        return out_path

    def extract(self, path: Path) -> list[Segment]:
        with tempfile.TemporaryDirectory(prefix="anon_doc_") as tmp:
            tmp_path = Path(tmp)
            converted = self._convert_to_docx(path, tmp_path)
            return self._docx.extract(converted)

    def write(
        self,
        src_path: Path,
        dst_path: Path,
        substitutions: list[SubstitutionRule],
    ) -> WriteReport:
        with tempfile.TemporaryDirectory(prefix="anon_doc_") as tmp:
            tmp_path = Path(tmp)
            converted = self._convert_to_docx(src_path, tmp_path)
            # Force a .docx extension on the destination.
            if dst_path.suffix.lower() == ".doc":
                dst_path = dst_path.with_suffix(".docx")
            report = self._docx.write(converted, dst_path, substitutions)
            report.is_lossy = True
            report.warnings.append(
                ".doc legacy converted to .docx via libreoffice (lossy)."
            )
            return report


__all__ = ["DocLegacyAdapter"]
