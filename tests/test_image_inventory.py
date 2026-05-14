"""Unit tests for the image inventory + decisions schema and I/O."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
import yaml
from PIL import Image

from anonymize.image_inventory import (
    FileInventory,
    ImageDecision,
    ImageInventory,
    ImageLocation,
    ImageRedactions,
    InventoryImage,
    RedactionRect,
    compute_image_id,
    filter_decisions,
    load_decisions,
    load_inventory,
    save_decisions,
    save_inventory,
    write_thumbnail,
)


def _png_bytes(size: tuple[int, int] = (10, 10), color: tuple[int, int, int] = (200, 0, 0)) -> bytes:
    img = Image.new("RGB", size, color)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_compute_image_id_is_stable_and_prefixed() -> None:
    raw = _png_bytes()
    a = compute_image_id(raw)
    b = compute_image_id(raw)
    assert a == b
    assert a.startswith("sha256:")
    # Different bytes -> different id.
    other = _png_bytes(color=(0, 200, 0))
    assert compute_image_id(other) != a


def test_inventory_roundtrip_yaml(tmp_path: Path) -> None:
    inv = ImageInventory(
        version=1,
        files=[
            FileInventory(
                file="input/report.pdf",
                file_sha256="abc123",
                images=[
                    InventoryImage(
                        image_id="sha256:11",
                        format="png",
                        width=800,
                        height=600,
                        location=ImageLocation(
                            kind="pdf",
                            page_index=2,
                            xref=17,
                            bbox=[10.0, 20.0, 110.0, 80.0],
                        ),
                        thumbnail=".anon/img_thumbs/11.jpg",
                        warnings=["cmyk"],
                    ),
                ],
            ),
        ],
    )
    dst = tmp_path / "image_inventory.yml"
    save_inventory(dst, inv)
    assert dst.exists()
    re = load_inventory(dst)
    assert len(re.files) == 1
    f = re.files[0]
    assert f.file == "input/report.pdf"
    assert f.file_sha256 == "abc123"
    assert len(f.images) == 1
    im = f.images[0]
    assert im.image_id == "sha256:11"
    assert im.format == "png"
    assert im.width == 800 and im.height == 600
    assert im.location.kind == "pdf"
    assert im.location.xref == 17
    assert im.warnings == ["cmyk"]


def test_inventory_default_when_file_missing(tmp_path: Path) -> None:
    inv = load_inventory(tmp_path / "missing.yml")
    assert inv.version == 1
    assert inv.files == []
    # generated_at is set even on the empty default so the GUI can
    # reason about freshness uniformly.
    assert inv.generated_at != ""


def test_decisions_roundtrip_yaml(tmp_path: Path) -> None:
    decisions = ImageRedactions(
        decisions={
            "sha256:11": ImageDecision(
                image_id="sha256:11",
                decision="redact",
                image_w=800,
                image_h=600,
                rects=[
                    RedactionRect(x=10, y=20, w=100, h=40, tool="blackout"),
                    RedactionRect(
                        x=200, y=200, w=80, h=80,
                        tool="blur", intensity=12,
                    ),
                    RedactionRect(
                        x=0, y=0, w=200, h=30,
                        tool="text_overlay",
                        text="REDACTED", font_size=18,
                        fg="#FFFFFF", bg="#000000",
                    ),
                ],
                edited_at="2026-05-10T12:00:00Z",
            ),
            "sha256:22": ImageDecision(
                image_id="sha256:22",
                decision="skip",
            ),
        }
    )
    dst = tmp_path / "image_redactions.yml"
    save_decisions(dst, decisions)
    re = load_decisions(dst)
    assert set(re.decisions) == {"sha256:11", "sha256:22"}
    d11 = re.get("sha256:11")
    assert d11 is not None
    assert d11.decision == "redact"
    assert len(d11.rects) == 3
    assert d11.rects[2].tool == "text_overlay"
    assert d11.rects[2].text == "REDACTED"
    d22 = re.get("sha256:22")
    assert d22 is not None
    assert d22.decision == "skip"


def test_decisions_default_when_file_missing(tmp_path: Path) -> None:
    d = load_decisions(tmp_path / "missing.yml")
    assert d.decisions == {}


def test_filter_decisions_keep_orphans_default(tmp_path: Path) -> None:
    inv = ImageInventory(files=[FileInventory(file="x.pdf")])
    decisions = ImageRedactions(
        decisions={
            "sha256:gone": ImageDecision(image_id="sha256:gone", decision="redact"),
        }
    )
    filtered = filter_decisions(decisions, inv)
    # Default: orphan kept, the GUI / pipeline decides when to GC.
    assert "sha256:gone" in filtered.decisions


def test_filter_decisions_can_prune_orphans() -> None:
    inv = ImageInventory(files=[
        FileInventory(file="x.pdf", images=[
            InventoryImage(
                image_id="sha256:live",
                format="png", width=10, height=10,
                location=ImageLocation(kind="pdf"),
            ),
        ]),
    ])
    decisions = ImageRedactions(
        decisions={
            "sha256:live": ImageDecision(image_id="sha256:live"),
            "sha256:gone": ImageDecision(image_id="sha256:gone"),
        }
    )
    filtered = filter_decisions(decisions, inv, keep_orphans=False)
    assert "sha256:live" in filtered.decisions
    assert "sha256:gone" not in filtered.decisions


def test_inventory_atomic_write_does_not_leave_tmp(tmp_path: Path) -> None:
    inv = ImageInventory()
    dst = tmp_path / "inv.yml"
    save_inventory(dst, inv)
    assert dst.exists()
    assert not (tmp_path / "inv.yml.tmp").exists()


def test_write_thumbnail_idempotent_and_jpeg(tmp_path: Path) -> None:
    raw = _png_bytes(size=(100, 100), color=(50, 100, 200))
    dst = tmp_path / "thumb.jpg"
    p1 = write_thumbnail(raw, "png", dst)
    assert p1 == dst
    assert dst.exists()
    # JPEG magic header.
    assert dst.read_bytes()[:2] == b"\xff\xd8"
    mtime1 = dst.stat().st_mtime
    # Second call: same path, must NOT rewrite.
    p2 = write_thumbnail(raw, "png", dst)
    assert p2 == dst
    assert dst.stat().st_mtime == mtime1


def test_yaml_roundtrip_preserves_dict_order(tmp_path: Path) -> None:
    """The on-disk YAML should be readable by humans; we expect the
    top-level keys ``version``, ``generated_at``, ``files`` to appear
    in that order. yaml.safe_dump with sort_keys=False respects insertion."""
    inv = ImageInventory(generated_at="2026-05-10T00:00:00Z")
    dst = tmp_path / "inv.yml"
    save_inventory(dst, inv)
    # generated_at gets re-stamped on save; just confirm the order.
    content = dst.read_text(encoding="utf-8")
    pos_version = content.find("version:")
    pos_generated = content.find("generated_at:")
    pos_files = content.find("files:")
    assert 0 <= pos_version < pos_generated < pos_files


def test_inventory_handles_corrupt_yaml(tmp_path: Path) -> None:
    dst = tmp_path / "inv.yml"
    dst.write_text("::: not yaml :::", encoding="utf-8")
    # Loader must NOT raise; returns empty inventory.
    inv = load_inventory(dst)
    assert inv.files == []


def test_redaction_rect_optional_fields_dropped_from_yaml(tmp_path: Path) -> None:
    """The on-disk YAML should NOT carry None-valued optional fields,
    keeping the file readable for humans (no dead ``intensity: null``
    rows on every blackout rect).
    """
    decisions = ImageRedactions(decisions={
        "sha256:1": ImageDecision(
            image_id="sha256:1",
            decision="redact",
            rects=[RedactionRect(x=0, y=0, w=10, h=10, tool="blackout")],
        ),
    })
    dst = tmp_path / "decisions.yml"
    save_decisions(dst, decisions)
    raw = dst.read_text(encoding="utf-8")
    assert "intensity" not in raw
    assert "text" not in raw or "tool: text_overlay" in raw  # text key only when text_overlay
