"""Regression test: download progress signal must accept >2 GB byte counts.

Before this fix, ``progress = Signal(str, str, int, int, float, int)``
silently overflowed when downloading large GGUF models (Gemma 4-E4B at
6.9 GB, Qwen3.6-27B at >20 GB) because PySide6 maps Python ``int`` to a
32-bit signed C int. The progress bar froze and the UI logged
``RuntimeWarning: libshiboken: Overflow``.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("ANONYMIZE_SKIP_WIZARD", "1")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    import sys

    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def test_progress_signal_accepts_more_than_2gb(qapp) -> None:
    from gui.model_manager_dialog import _DownloadSignals

    sigs = _DownloadSignals()
    received: list[tuple] = []
    sigs.progress.connect(lambda *args: received.append(args))

    # 6.9 GB (gemma-4-E4B-it-UD-Q6_K_XL.gguf), was the file in the
    # user's bug report.
    sigs.progress.emit(
        "unsloth/gemma-4-E4B-it-GGUF",
        "gemma-4-E4B-it-UD-Q6_K_XL.gguf",
        3_450_000_000,
        6_900_000_000,
        51_900_000.0,
        72,
    )
    qapp.processEvents()
    assert len(received) == 1
    repo, fn, done, total, _speed, eta = received[0]
    assert repo == "unsloth/gemma-4-E4B-it-GGUF"
    assert done == 3_450_000_000
    assert total == 6_900_000_000
    assert eta == 72


def test_progress_signal_accepts_just_over_int32_max(qapp) -> None:
    from gui.model_manager_dialog import _DownloadSignals

    sigs = _DownloadSignals()
    received: list[tuple] = []
    sigs.progress.connect(lambda *args: received.append(args))

    # The exact boundary that used to overflow (2^31 == 2_147_483_648).
    sigs.progress.emit("r", "f", 2_147_483_648, 4_000_000_000, 1.0, 1)
    qapp.processEvents()
    assert len(received) == 1
    assert received[0][2] == 2_147_483_648
    assert received[0][3] == 4_000_000_000
