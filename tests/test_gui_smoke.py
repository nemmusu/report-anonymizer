"""Offscreen GUI smoke tests (don't require pytest-qt-specific markers)."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("ANONYMIZE_SKIP_WIZARD", "1")


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    import sys
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def test_main_window_constructs(qapp) -> None:
    from gui.app import MainWindow
    w = MainWindow()
    w.show()
    assert w.isVisible()
    w.close()


def test_preset_gallery_renders(qapp) -> None:
    from gui.preset_gallery import PresetGallery
    g = PresetGallery()
    g.show()
    assert len(g._cards) >= 1
    g.close()



def test_about_dialog(qapp) -> None:
    from gui.about_dialog import AboutDialog
    d = AboutDialog()
    d.show()
    d.close()


def test_shortcuts_overlay(qapp) -> None:
    from gui.shortcuts_overlay import ShortcutsOverlay
    o = ShortcutsOverlay()
    o.show()
    o.close()
