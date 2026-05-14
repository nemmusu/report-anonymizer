"""RTF adapter via pandoc roundtrip.

We convert ``rtf -> markdown -> anonymize -> markdown -> rtf``. This is a
lossy round-trip (custom styles / non-standard control words are flattened
to pandoc's markdown subset), so the adapter sets ``is_lossy=True``.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .base import (
    FormatAdapter,
    Segment,
    SubstitutionRule,
    WriteEvent,
    WriteReport,
    apply_to_text,
)


def _check_pandoc() -> str:
    binp = shutil.which("pandoc")
    if not binp:  # pragma: no cover
        raise RuntimeError("pandoc is required for the rtf adapter")
    return binp


class RtfAdapter(FormatAdapter):
    name = "rtf"
    extensions = {".rtf"}
    mimes = {"application/rtf", "text/rtf"}
    is_lossy = True

    def __init__(self) -> None:
        self._pandoc = _check_pandoc()

    def _to_markdown(self, src: Path) -> str:
        proc = subprocess.run(
            [self._pandoc, "-f", "rtf", "-t", "markdown", "--wrap=none", str(src)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"pandoc rtf->md failed for {src}: {proc.stderr.strip()[:300]}"
            )
        return proc.stdout

    def _from_markdown(self, md: str, dst: Path) -> None:
        proc = subprocess.run(
            [self._pandoc, "-f", "markdown", "-t", "rtf", "-o", str(dst)],
            input=md,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"pandoc md->rtf failed for {dst}: {proc.stderr.strip()[:300]}"
            )

    def extract(self, path: Path) -> list[Segment]:
        md = self._to_markdown(path)
        return [Segment(seg_id="0", text=md)]

    def write(
        self,
        src_path: Path,
        dst_path: Path,
        substitutions: list[SubstitutionRule],
    ) -> WriteReport:
        md = self._to_markdown(src_path)
        new_md, events = apply_to_text(md, substitutions, seg_id="0")
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(suffix=".rtf", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            self._from_markdown(new_md, tmp_path)
            shutil.copy2(tmp_path, dst_path)
        finally:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
        report = WriteReport(file_rel=str(dst_path), events=events, is_lossy=True)
        report.warnings.append("RTF roundtrip via pandoc is lossy.")
        return report


__all__ = ["RtfAdapter"]
