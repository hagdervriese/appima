"""Qt 6 application bootstrapper for appima."""

from __future__ import annotations

import sys
from pathlib import Path

from appima import __version__
from appima.core.runtime import gui_asset_path

_ICON_PATH = gui_asset_path("appima.svg")


def launch() -> int:
    """Start the PySide6 GUI, printing a helpful install hint if PySide6 is missing."""
    try:
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print(
            "PySide6 (Qt 6) is required to run the appima GUI.\n"
            "Install it on Ubuntu/Kubuntu/Debian with:\n"
            "  sudo apt install python3-pyside6.qtwidgets python3-pyside6.qtgui python3-pyside6.qtcore\n"
            "Or via pip:\n"
            "  pip install PySide6\n\n"
            "You can still use all CLI commands right away, e.g. `python3 -m appima --help`.",
            file=sys.stderr,
        )
        return 1

    from appima.gui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("appima")
    app.setApplicationDisplayName("appima")
    app.setApplicationVersion(__version__)
    app.setDesktopFileName("appima")

    if _ICON_PATH.is_file():
        app.setWindowIcon(QIcon(str(_ICON_PATH)))

    window = MainWindow()
    window.show()
    return app.exec()
