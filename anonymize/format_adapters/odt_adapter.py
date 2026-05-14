"""ODT adapter built on odfpy.

Walks the ODT XML tree picking up text in paragraphs (``text:p``), headings
(``text:h``), spans (``text:span``), and list items. Substitutions are
applied at the level of leaf text nodes; we then redistribute through the
same node-merge trick as docx/pptx.
"""
from __future__ import annotations

from pathlib import Path

from .base import (
    FormatAdapter,
    Segment,
    SubstitutionRule,
    WriteEvent,
    WriteReport,
    apply_to_text,
)


def _iter_text_blocks(doc):
    """Yield (seg_id, block_element) for every paragraph-like element."""
    from odf.element import Element  # type: ignore
    from odf import text as odf_text  # type: ignore

    counter = {"i": 0}

    def walk(node, prefix: str):
        if not isinstance(node, Element):
            return
        if node.qname[1] in ("p", "h", "list-item"):
            counter["i"] += 1
            yield f"{prefix}#{counter['i']}", node
            return
        for child in node.childNodes:
            yield from walk(child, prefix)

    yield from walk(doc.text, "body")


def _block_text(block) -> str:
    """Concatenate all text content in a block."""
    from odf.element import Text, Element  # type: ignore

    out: list[str] = []

    def walk(n):
        if isinstance(n, Text):
            out.append(n.data or "")
        elif isinstance(n, Element):
            for c in n.childNodes:
                walk(c)

    walk(block)
    return "".join(out)


def _set_block_text(block, full_text: str) -> None:
    """Replace text content of a block with ``full_text``.

    The strategy is similar to docx/pptx: split ``full_text`` across the
    existing leaf Text nodes in document order, preserving the surrounding
    inline elements (spans, links, ...). Trailing nodes that overflow the new
    length are emptied; the last node receives the remainder.
    """
    from odf.element import Text, Element  # type: ignore

    leaves: list[Text] = []

    def walk(n):
        if isinstance(n, Text):
            leaves.append(n)
        elif isinstance(n, Element):
            for c in n.childNodes:
                walk(c)

    walk(block)
    if not leaves:
        from odf.text import Span  # type: ignore

        block.addText(full_text)
        return

    pos = 0
    n = len(full_text)
    for idx, leaf in enumerate(leaves):
        original = leaf.data or ""
        if pos >= n:
            leaf.data = ""
            continue
        if idx == len(leaves) - 1:
            leaf.data = full_text[pos:]
            return
        end = min(pos + len(original), n)
        leaf.data = full_text[pos:end]
        pos = end


class OdtAdapter(FormatAdapter):
    name = "odt"
    extensions = {".odt"}
    mimes = {"application/vnd.oasis.opendocument.text"}

    def __init__(self) -> None:
        try:
            from odf.opendocument import load  # type: ignore  # noqa: F401
        except Exception as e:  # pragma: no cover
            raise RuntimeError(f"odfpy is required for the odt adapter: {e}")

    def extract(self, path: Path) -> list[Segment]:
        from odf.opendocument import load  # type: ignore

        doc = load(str(path))
        out: list[Segment] = []
        for seg_id, block in _iter_text_blocks(doc):
            text = _block_text(block)
            if not text:
                continue
            out.append(Segment(seg_id=seg_id, text=text))
        return out

    def write(
        self,
        src_path: Path,
        dst_path: Path,
        substitutions: list[SubstitutionRule],
    ) -> WriteReport:
        from odf.opendocument import load  # type: ignore

        doc = load(str(src_path))
        events: list[WriteEvent] = []
        for seg_id, block in _iter_text_blocks(doc):
            text = _block_text(block)
            if not text:
                continue
            new, ev = apply_to_text(text, substitutions, seg_id=seg_id)
            if new == text:
                continue
            _set_block_text(block, new)
            events.extend(ev)
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(dst_path))
        return WriteReport(file_rel=str(dst_path), events=events)


__all__ = ["OdtAdapter"]
