"""Format adapter ABC and shared dataclasses.

An adapter has two responsibilities:

* ``extract(path)`` -> list[Segment]  – returns the textual content of the file
  as a list of segments. The "shape" of a segment is format-specific (a Word
  Run, an Excel cell, a paragraph...) but exposes the same triple
  ``(seg_id, text, meta)``.
* ``write(src, dst, substitutions)``  – produces the anonymized file at ``dst``
  applying the substitutions while preserving the native structure.

The applier never mutates the source file, only reads it and writes a new
version.
"""
from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class Segment:
    """A unit of text extracted from a file by an adapter."""

    seg_id: str  # identity stable enough to roundtrip the substitution
    text: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class WriteEvent:
    """One concrete substitution application recorded for the diff view."""

    seg_id: str
    orig_off: int
    orig_len: int
    anon_off: int
    anon_len: int
    from_: str
    to: str
    category: str = ""
    mapping_id: str = ""
    tier: str = ""
    # Page index + on-page rectangle(s) for the original substituted
    # text. Populated by the PDF adapters; ``None`` for purely textual
    # adapters that have no notion of layout. Used by the rendered
    # diff view to overlay highlights at the correct position on
    # rasterised pages.
    page: Optional[int] = None
    rects: list[tuple[float, float, float, float]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "seg_id": self.seg_id,
            "orig_off": self.orig_off,
            "orig_len": self.orig_len,
            "anon_off": self.anon_off,
            "anon_len": self.anon_len,
            "from": self.from_,
            "to": self.to,
            "category": self.category,
            "mapping_id": self.mapping_id,
            "tier": self.tier,
        }
        if self.page is not None:
            d["page"] = self.page
        if self.rects:
            d["rects"] = [list(r) for r in self.rects]
        return d


@dataclass
class WriteReport:
    """Summary of a single ``adapter.write`` call."""

    file_rel: str
    events: list[WriteEvent] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    is_lossy: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file_rel,
            "events": [e.to_dict() for e in self.events],
            "warnings": list(self.warnings),
            "is_lossy": self.is_lossy,
        }


@dataclass
class InventoryImageRaw:
    """Per-format adapter -> inventory transport object.

    The adapter walks its container, extracts each embedded raster
    image as raw bytes, and yields one of these. The image-inventory
    layer then computes ``image_id`` (sha256 of bytes) and builds the
    user-facing :class:`InventoryImage` from this transport plus the
    location dict the adapter filled in.
    """

    raw_bytes: bytes                # the original encoded image bytes
    fmt: str                        # "png" / "jpeg" / "tiff" / ...
    width: int
    height: int
    location: dict[str, Any]        # serialised ImageLocation, kind-specific keys
    warnings: list[str] = field(default_factory=list)


@dataclass
class ImageReport:
    """Summary of a single ``adapter.apply_image_redactions`` call.

    Mirrors the shape of :class:`WriteReport` so the apply pass can
    fold image-redaction outcomes into the same per-file event log
    consumed by the GUI Diff view and the verifier.
    """

    file_rel: str
    applied: int = 0                # number of images actually rewritten
    skipped: int = 0                # decisions == "skip" or "defer"
    untouched: int = 0              # images with no decision in the file
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file_rel,
            "applied": self.applied,
            "skipped": self.skipped,
            "untouched": self.untouched,
            "warnings": list(self.warnings),
        }


@dataclass
class SubstitutionRule:
    """A canonical substitution as applied by the adapters.

    The applier compiles ``substitution_map.yml`` into a list of these. The
    adapter consumes them; it must apply ``longest-first`` order.
    """

    from_: str
    to: str
    category: str = ""
    mapping_id: str = ""
    tier: str = ""
    case_insensitive: bool = False


class FormatAdapter(ABC):
    """Interface every format adapter must implement."""

    extensions: set[str] = set()
    mimes: set[str] = set()
    name: str = "base"
    is_lossy: bool = False

    @abstractmethod
    def extract(self, path: Path) -> list[Segment]:  # pragma: no cover - abstract
        ...

    @abstractmethod
    def write(
        self,
        src_path: Path,
        dst_path: Path,
        substitutions: list[SubstitutionRule],
    ) -> WriteReport:  # pragma: no cover - abstract
        ...

    def inventory_images(self, path: Path) -> list["InventoryImageRaw"]:
        """Enumerate embedded raster images for the image-redaction
        pipeline.

        Returns a list of ``InventoryImageRaw`` (a thin per-format
        record carrying the raw image bytes plus enough metadata
        for the inventory to compute ``image_id`` and the location
        descriptor). Default: no images. Per-format adapters that
        support embedded images (PDF / DOCX / PPTX) override this.

        The image flow is intentionally additive: an adapter that
        does not override returns nothing, the inventory simply
        records "no images for this file", and the apply pass is a
        no-op. The textual flow is unaffected.
        """
        return []

    def apply_image_redactions(
        self,
        dst_path: Path,
        decisions_for_file: dict[str, Any],
    ) -> "ImageReport":
        """Apply operator image redactions to a freshly-written file.

        ``decisions_for_file`` maps ``image_id`` -> ``ImageDecision``
        for the images that the inventory recorded for THIS file.
        The default no-op simply reports zero applied / zero
        skipped, so non-image formats inherit a safe pass-through
        without any per-format glue. PDF / DOCX / PPTX override.
        """
        return ImageReport(file_rel=str(dst_path))

    def identity_check(self, path: Path) -> bool:
        """Sanity check: extract+write with no substitutions must roundtrip the
        file's textual content. Subclasses override if a tighter check is
        desired (byte-for-byte equality)."""
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=path.suffix, delete=False) as tmp:
            dst = Path(tmp.name)
        try:
            self.write(path, dst, [])
            before = "\n".join(s.text for s in self.extract(path))
            after = "\n".join(s.text for s in self.extract(dst))
            return before == after
        finally:
            try:
                dst.unlink()
            except FileNotFoundError:
                pass


class NullAdapter(FormatAdapter):
    """Adapter for files we cannot or should not anonymize.

    ``extract`` returns no segments; ``write`` copies the source file as-is.
    """

    name = "null"

    def extract(self, path: Path) -> list[Segment]:
        return []

    def write(
        self,
        src_path: Path,
        dst_path: Path,
        substitutions: list[SubstitutionRule],
    ) -> WriteReport:
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        if src_path.resolve() != dst_path.resolve():
            shutil.copy2(src_path, dst_path)
        return WriteReport(file_rel=str(dst_path), events=[], warnings=[])


def apply_to_text(
    text: str,
    substitutions: list[SubstitutionRule],
    *,
    seg_id: str = "0",
) -> tuple[str, list[WriteEvent]]:
    """Apply substitutions to a flat string using longest-match-first order.

    Returns ``(new_text, events)`` where each event records the substring on
    both the original and the anonymized side. Greedy left-to-right scan: at
    each position we try the longest unmatched substitution first.
    """
    if not text or not substitutions:
        return text, []
    rules = sorted(substitutions, key=lambda r: -len(r.from_))
    out_parts: list[str] = []
    events: list[WriteEvent] = []
    i = 0
    n = len(text)
    out_pos = 0
    while i < n:
        matched: SubstitutionRule | None = None
        match_len = 0
        for r in rules:
            if not r.from_:
                continue
            flen = len(r.from_)
            if i + flen > n:
                continue
            if r.case_insensitive:
                if text[i : i + flen].lower() == r.from_.lower():
                    matched = r
                    match_len = flen
                    break
            else:
                if text[i : i + flen] == r.from_:
                    matched = r
                    match_len = flen
                    break
        if matched is not None:
            out_parts.append(matched.to)
            events.append(
                WriteEvent(
                    seg_id=seg_id,
                    orig_off=i,
                    orig_len=match_len,
                    anon_off=out_pos,
                    anon_len=len(matched.to),
                    from_=text[i : i + match_len],
                    to=matched.to,
                    category=matched.category,
                    mapping_id=matched.mapping_id,
                    tier=matched.tier,
                )
            )
            i += match_len
            out_pos += len(matched.to)
        else:
            out_parts.append(text[i])
            i += 1
            out_pos += 1
    return "".join(out_parts), events


__all__ = [
    "Segment",
    "WriteEvent",
    "WriteReport",
    "ImageReport",
    "InventoryImageRaw",
    "SubstitutionRule",
    "FormatAdapter",
    "NullAdapter",
    "apply_to_text",
]
