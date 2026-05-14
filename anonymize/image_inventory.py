"""Image inventory: scan-time discovery + decisions persistence.

Two YAML artefacts under ``<output_dir>/`` describe the image-redaction
state of a project:

* ``image_inventory.yml`` is auto-built at scan time. It lists every
  embedded image found in the input files, identified by sha256 of
  the raw bytes (``image_id``), with per-occurrence location info
  (which file, which page or slide, what xref or shape id). The
  inventory is recomputed on every scan so newly-added images appear
  immediately and removed images vanish; it is never edited by the
  operator.

* ``image_redactions.yml`` holds the operator decisions, keyed by
  ``image_id``. Survives re-scans by design: as long as the image
  bytes do not change, the same id resolves to the same decision
  (the file path or page index could shift between runs without
  invalidating the choice).

This module ships the dataclasses, the YAML I/O, and the merge logic
that combines a freshly-built inventory with the existing decisions
file. The per-format extraction (PDF / DOCX / PPTX -> image bytes +
location) lives on each format adapter, this module is format-agnostic.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Iterable, Literal, Optional

import yaml

try:
    from PIL import Image  # type: ignore
except Exception:  # pragma: no cover - PIL ships transitively via WeasyPrint
    Image = None  # type: ignore


LocationKind = Literal["pdf", "docx", "pptx", "odt", "xlsx"]
Decision = Literal["redact", "skip", "defer"]
Tool = Literal["blackout", "blur", "pixelate", "text_overlay"]


# ---- inventory dataclasses ------------------------------------------

@dataclass
class ImageLocation:
    """Where exactly an image lives inside its container.

    Each format uses a different addressing scheme; we keep them in
    a tagged union via ``kind`` so the per-format adapter can
    reconstruct the in-place lookup without needing the original
    extraction context.
    """
    kind: LocationKind
    # PDF: identifies the image in the cross-reference table; the
    # bbox is for the GUI overlay only (apply uses xref).
    page_index: Optional[int] = None
    xref: Optional[int] = None
    bbox: Optional[list[float]] = None
    # DOCX inline shape: stable index in document.inline_shapes.
    inline_shape_index: Optional[int] = None
    # PPTX: slide index + shape id.
    slide_index: Optional[int] = None
    shape_id: Optional[int] = None
    # Generic relationship id (used for DOCX floating-shape fallback).
    rel_id: Optional[str] = None

    def to_dict(self) -> dict:
        d: dict = {"kind": self.kind}
        for k in (
            "page_index", "xref", "bbox",
            "inline_shape_index",
            "slide_index", "shape_id",
            "rel_id",
        ):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ImageLocation":
        return cls(
            kind=d.get("kind", "pdf"),
            page_index=d.get("page_index"),
            xref=d.get("xref"),
            bbox=d.get("bbox"),
            inline_shape_index=d.get("inline_shape_index"),
            slide_index=d.get("slide_index"),
            shape_id=d.get("shape_id"),
            rel_id=d.get("rel_id"),
        )


@dataclass
class InventoryImage:
    """One occurrence of an image inside one container file.

    Two ``InventoryImage`` instances with the same ``image_id`` but
    different ``location`` represent the same image bytes embedded
    at different positions (e.g. a logo on every page).
    """
    image_id: str                       # sha256:<hex>
    format: str                         # "png" / "jpeg" / "tiff" / ...
    width: int
    height: int
    location: ImageLocation
    thumbnail: Optional[str] = None     # path relative to output_dir
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {
            "image_id": self.image_id,
            "format": self.format,
            "width": self.width,
            "height": self.height,
            "location": self.location.to_dict(),
        }
        if self.thumbnail:
            d["thumbnail"] = self.thumbnail
        if self.warnings:
            d["warnings"] = list(self.warnings)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "InventoryImage":
        return cls(
            image_id=d["image_id"],
            format=d.get("format", "png"),
            width=int(d.get("width", 0)),
            height=int(d.get("height", 0)),
            location=ImageLocation.from_dict(d.get("location", {})),
            thumbnail=d.get("thumbnail"),
            warnings=list(d.get("warnings") or []),
        )


@dataclass
class FileInventory:
    """All images found in one input file."""
    file: str                           # input path, relative to project root if possible
    file_sha256: Optional[str] = None
    images: list[InventoryImage] = field(default_factory=list)

    def to_dict(self) -> dict:
        d: dict = {"file": self.file}
        if self.file_sha256:
            d["file_sha256"] = self.file_sha256
        d["images"] = [im.to_dict() for im in self.images]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "FileInventory":
        return cls(
            file=d["file"],
            file_sha256=d.get("file_sha256"),
            images=[InventoryImage.from_dict(x) for x in (d.get("images") or [])],
        )


@dataclass
class ImageInventory:
    """Top-level container for the YAML root."""
    version: int = 1
    generated_at: str = ""
    files: list[FileInventory] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "generated_at": self.generated_at,
            "files": [f.to_dict() for f in self.files],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ImageInventory":
        return cls(
            version=int(d.get("version", 1)),
            generated_at=str(d.get("generated_at", "")),
            files=[FileInventory.from_dict(f) for f in (d.get("files") or [])],
        )

    def all_image_ids(self) -> set[str]:
        return {im.image_id for f in self.files for im in f.images}


# ---- decisions dataclasses ------------------------------------------

@dataclass(frozen=True)
class RedactionRect:
    x: int
    y: int
    w: int
    h: int
    tool: Tool
    intensity: Optional[int] = None
    text: Optional[str] = None
    font_size: Optional[int] = None
    fg: Optional[str] = None
    bg: Optional[str] = None

    def to_dict(self) -> dict:
        d: dict = {"x": self.x, "y": self.y, "w": self.w, "h": self.h, "tool": self.tool}
        for k in ("intensity", "text", "font_size", "fg", "bg"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "RedactionRect":
        return cls(
            x=int(d["x"]), y=int(d["y"]),
            w=int(d["w"]), h=int(d["h"]),
            tool=d.get("tool", "blackout"),
            intensity=d.get("intensity"),
            text=d.get("text"),
            font_size=d.get("font_size"),
            fg=d.get("fg"),
            bg=d.get("bg"),
        )


@dataclass
class ImageDecision:
    """Operator decision for one ``image_id``."""
    image_id: str
    decision: Decision = "defer"
    image_w: Optional[int] = None
    image_h: Optional[int] = None
    rects: list[RedactionRect] = field(default_factory=list)
    edited_at: Optional[str] = None

    def to_dict(self) -> dict:
        d: dict = {"decision": self.decision}
        if self.image_w is not None:
            d["image_w"] = self.image_w
        if self.image_h is not None:
            d["image_h"] = self.image_h
        if self.rects:
            d["rects"] = [r.to_dict() for r in self.rects]
        if self.edited_at:
            d["edited_at"] = self.edited_at
        return d

    @classmethod
    def from_dict(cls, image_id: str, d: dict) -> "ImageDecision":
        return cls(
            image_id=image_id,
            decision=d.get("decision", "defer"),
            image_w=d.get("image_w"),
            image_h=d.get("image_h"),
            rects=[RedactionRect.from_dict(r) for r in (d.get("rects") or [])],
            edited_at=d.get("edited_at"),
        )


@dataclass
class ImageRedactions:
    version: int = 1
    decisions: dict[str, ImageDecision] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "decisions": {k: v.to_dict() for k, v in self.decisions.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ImageRedactions":
        decisions_raw = d.get("decisions") or {}
        decisions: dict[str, ImageDecision] = {}
        for k, v in decisions_raw.items():
            decisions[k] = ImageDecision.from_dict(k, v or {})
        return cls(version=int(d.get("version", 1)), decisions=decisions)

    def get(self, image_id: str) -> Optional[ImageDecision]:
        return self.decisions.get(image_id)


# ---- I/O ------------------------------------------------------------

def load_inventory(path: Path) -> ImageInventory:
    """Load ``image_inventory.yml`` or return an empty inventory."""
    if not path.exists():
        return ImageInventory(generated_at=_now_iso())
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return ImageInventory(generated_at=_now_iso())
    if not isinstance(data, dict):
        return ImageInventory(generated_at=_now_iso())
    return ImageInventory.from_dict(data)


def save_inventory(path: Path, inv: ImageInventory) -> None:
    """Atomic-write ``inv`` to ``path``."""
    inv.generated_at = _now_iso()
    _atomic_yaml_write(path, inv.to_dict())


def load_decisions(path: Path) -> ImageRedactions:
    """Load ``image_redactions.yml`` or return an empty decisions set."""
    if not path.exists():
        return ImageRedactions()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return ImageRedactions()
    if not isinstance(data, dict):
        return ImageRedactions()
    return ImageRedactions.from_dict(data)


def save_decisions(path: Path, decisions: ImageRedactions) -> None:
    """Atomic-write ``decisions`` to ``path``."""
    _atomic_yaml_write(path, decisions.to_dict())


# ---- helpers --------------------------------------------------------

def compute_image_id(raw_bytes: bytes) -> str:
    """Stable identifier for an embedded image: sha256 of its raw bytes."""
    return "sha256:" + hashlib.sha256(raw_bytes).hexdigest()


def write_thumbnail(
    raw_bytes: bytes,
    fmt_hint: str,
    dst_path: Path,
    *,
    max_size: int = 256,
) -> Optional[Path]:
    """Render and persist a small thumbnail. Returns the absolute path
    or None if PIL cannot decode the image (rare fallback path).

    Idempotent: if the destination already exists, returns the path
    without re-rendering. The caller decides whether to skip the
    write based on cache presence (we leave that to the caller so it
    can also collect statistics).
    """
    if Image is None:
        return None
    if dst_path.exists():
        return dst_path
    try:
        img = Image.open(BytesIO(raw_bytes))
        img.load()
    except Exception:
        return None
    # JPEG cannot store an alpha channel, so any mode that carries
    # transparency (RGBA / LA / palette-with-alpha) must be flattened
    # against a solid background first. Without this every PNG with a
    # logo + transparent background silently failed to thumbnail and
    # the gallery showed the generic placeholder icon. Indexed (P) and
    # CMYK images get the usual conversion to plain RGB. The white
    # background is the safe choice for screenshots / docs / logos
    # where the alpha is the page background.
    if img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info
    ):
        rgba = img.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.split()[-1])
        img = flat
    elif img.mode in ("CMYK", "P", "I", "F"):
        img = img.convert("RGB")
    elif img.mode != "RGB":
        # 1-bit, gray, etc. — coerce to RGB so JPEG always works.
        img = img.convert("RGB")
    img.thumbnail((max_size, max_size))
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst_path.with_suffix(dst_path.suffix + ".tmp")
    try:
        img.save(tmp, format="JPEG", quality=70, optimize=True)
        tmp.replace(dst_path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        return None
    return dst_path


def merge_inventory(
    fresh: ImageInventory,
    previous: ImageInventory,
) -> ImageInventory:
    """Replace the inventory with the fresh scan. Pure replace by
    design: the inventory is a snapshot of the current source files,
    not a cumulative log. The previous ``image_redactions.yml`` is
    untouched and handled separately by ``filter_decisions``.
    """
    fresh.generated_at = _now_iso()
    return fresh


def filter_decisions(
    decisions: ImageRedactions,
    inventory: ImageInventory,
    *,
    keep_orphans: bool = True,
) -> ImageRedactions:
    """Return a copy of ``decisions``, optionally pruning entries
    whose ``image_id`` is no longer present in ``inventory``.

    ``keep_orphans=True`` (default) is the conservative behaviour:
    operator decisions outlive temporary source mutations (the user
    might re-import the same PDF in a slightly different version
    that does not yet contain the redacted image; we do NOT want to
    silently lose their work). A future ``gc_image_decisions()``
    step can prune orphan ids that have been gone for N days.
    """
    if keep_orphans:
        return ImageRedactions(version=decisions.version, decisions=dict(decisions.decisions))
    live = inventory.all_image_ids()
    return ImageRedactions(
        version=decisions.version,
        decisions={k: v for k, v in decisions.decisions.items() if k in live},
    )


def _atomic_yaml_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = yaml.safe_dump(
        payload,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "Decision",
    "FileInventory",
    "ImageDecision",
    "ImageInventory",
    "ImageLocation",
    "ImageRedactions",
    "InventoryImage",
    "LocationKind",
    "RedactionRect",
    "Tool",
    "compute_image_id",
    "filter_decisions",
    "load_decisions",
    "load_inventory",
    "merge_inventory",
    "save_decisions",
    "save_inventory",
    "write_thumbnail",
]
