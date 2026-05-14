"""GUI entry point: ``python -m gui.main``."""
from __future__ import annotations

import sys
import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Suppress transient console-window flashes on Windows. This MUST be
# installed before any code path can call subprocess.Popen / .run for
# the patch to take effect; doing it here (the GUI entry point) means
# every child process spawned by the GUI process tree -- pandoc,
# pdftotext, libreoffice, nvidia-smi, llama-server, etc. -- inherits
# the no-console-window behaviour without us having to touch each
# call site individually. No-op on POSIX.
from gui._win_subprocess_patch import install as _install_no_window_patch  # noqa: E402

_install_no_window_patch()

from gui.app import MainWindow  # noqa: E402
from gui.icons import app_icon  # noqa: E402
from gui.splash import Splash  # noqa: E402
from gui.theme import qss  # noqa: E402


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("report-anonymizer")
    app.setOrganizationName("report-anonymizer")
    app.setApplicationDisplayName("Report Anonymizer")
    app.setWindowIcon(app_icon(64))
    app.setStyleSheet(qss())
    app.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs, False)

    splash = Splash()
    splash.show()
    splash.update_message("Loading…")
    app.processEvents()

    splash.update_message("Loading workers…")
    app.processEvents()
    win = MainWindow()
    splash.update_message("Ready")
    app.processEvents()
    win.show()
    splash.finish(win)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
