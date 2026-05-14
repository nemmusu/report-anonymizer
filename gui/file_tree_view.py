"""File tree with tristate include/exclude checkboxes.

Used by :class:`ScanPreviewDialog`. Driven from a :class:`ScanResult` to
build the directory hierarchy.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem


class FileTreeView(QTreeWidget):
    exclusions_changed = Signal(list)  # list[Path]

    def __init__(self, root: Path, scan_result, parent=None) -> None:
        super().__init__(parent)
        self.root = Path(root).resolve()
        self.scan = scan_result
        self.setHeaderLabels(["Path", "Files", "Size"])
        self.setColumnWidth(0, 420)
        self._items_by_path: dict[Path, QTreeWidgetItem] = {}
        self._build()
        self.itemChanged.connect(self._on_item_changed)

    def _ensure_dir_item(self, path: Path) -> QTreeWidgetItem:
        if path == self.root:
            if path not in self._items_by_path:
                top = QTreeWidgetItem([str(self.root.name) or str(self.root), "", ""])
                top.setFlags(top.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsAutoTristate)
                top.setCheckState(0, Qt.CheckState.Checked)
                top.setData(0, Qt.ItemDataRole.UserRole, str(self.root))
                self.addTopLevelItem(top)
                self._items_by_path[path] = top
                return top
            return self._items_by_path[path]
        if path in self._items_by_path:
            return self._items_by_path[path]
        parent = self._ensure_dir_item(path.parent)
        item = QTreeWidgetItem([path.name, "", ""])
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsAutoTristate)
        item.setCheckState(0, Qt.CheckState.Checked)
        item.setData(0, Qt.ItemDataRole.UserRole, str(path))
        parent.addChild(item)
        self._items_by_path[path] = item
        return item

    def _build(self) -> None:
        for sf in self.scan.files:
            try:
                full = (self.root / sf.rel).resolve()
            except Exception:
                continue
            parent = self._ensure_dir_item(full.parent)
            child = QTreeWidgetItem([full.name, "", _human(sf.size)])
            child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            child.setCheckState(0, Qt.CheckState.Checked)
            child.setData(0, Qt.ItemDataRole.UserRole, str(full))
            parent.addChild(child)
            self._items_by_path[full] = child
        # Aggregate counts up
        self._aggregate(self.invisibleRootItem())
        self.expandToDepth(1)

    def _aggregate(self, node: QTreeWidgetItem) -> tuple[int, int]:
        if node.childCount() == 0:
            return 1, _size_of(node) or 0
        total_files = 0
        total_size = 0
        for i in range(node.childCount()):
            ch = node.child(i)
            cnt, sz = self._aggregate(ch)
            total_files += cnt
            total_size += sz
        if node is not self.invisibleRootItem():
            node.setText(1, str(total_files))
            node.setText(2, _human(total_size))
        return total_files, total_size

    def _on_item_changed(self, item: QTreeWidgetItem, col: int) -> None:
        if col != 0:
            return
        excluded: list[Path] = []
        for path, it in self._items_by_path.items():
            if it.checkState(0) == Qt.CheckState.Unchecked:
                excluded.append(path)
        self.exclusions_changed.emit([p for p in excluded if not p.is_dir()])


def _human(n: int) -> str:
    if n <= 0:
        return ""
    units = ("B", "KB", "MB", "GB", "TB")
    i = 0
    f = float(n)
    while f >= 1024 and i < len(units) - 1:
        f /= 1024
        i += 1
    return f"{f:.1f} {units[i]}"


def _size_of(it: QTreeWidgetItem) -> int:
    text = it.text(2)
    if not text:
        return 0
    try:
        num = float(text.split(" ")[0])
        unit = text.split(" ")[1]
    except Exception:
        return 0
    mul = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}.get(unit, 1)
    return int(num * mul)


__all__ = ["FileTreeView"]
