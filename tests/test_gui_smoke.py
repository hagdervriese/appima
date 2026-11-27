"""Headless Qt 6 GUI smoke tests (runs automatically when PySide6 is available)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    HAS_PYSIDE6 = True
except ImportError:
    HAS_PYSIDE6 = False

from appima.core.config import AppimaPaths
from appima.core.manager import AppManager


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed in this environment")
class GuiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def _make_manager(self, root: Path) -> AppManager:
        paths = AppimaPaths(
            config_dir=root / "config" / "appima",
            data_dir=root / "data" / "appima",
            cache_dir=root / "cache" / "appima",
            applications_dir=root / "data" / "applications",
            systemd_user_dir=root / "config" / "systemd" / "user",
        )
        mgr = AppManager(paths=paths, refresh_desktop=False)
        mgr.update_config(apps_dir=root / "Apps")
        return mgr

    def test_main_window_and_dialogs_open_cleanly(self) -> None:
        from appima.gui.add_dialog import AddAppDialog
        from appima.gui.details_dialog import AppDetailsDialog
        from appima.gui.main_window import AppRowWidget, MainWindow
        from appima.gui.settings_dialog import SettingsDialog

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mgr = self._make_manager(root)
            sample = root / "Sample-1.2.3.AppImage"
            sample.write_bytes(b"\x7fELF\x02\x01\x01" + b"\x00" * 57 + b"xyz" * 16)
            rec = mgr.install_app(str(sample))

            win = MainWindow(manager=mgr)
            self.assertEqual(win.list_layout.count(), 2)  # 1 row + trailing stretch
            row = win.list_layout.itemAt(0).widget()
            self.assertIsInstance(row, AppRowWidget)

            # Verify Details dialog instantiates cleanly
            details = AppDetailsDialog(mgr, rec, parent=win)
            self.assertEqual(details.windowTitle(), rec.name)
            details.close()

            # Verify clicking the row's Details button actually triggers _open_details
            opened = []
            orig_exec = AppDetailsDialog.exec

            def _fake_exec(dlg: AppDetailsDialog) -> int:
                opened.append(dlg.record.id)
                return AppDetailsDialog.DialogCode.Rejected

            AppDetailsDialog.exec = _fake_exec  # type: ignore[method-assign]
            try:
                assert isinstance(row, AppRowWidget)
                row.details_btn.click()
                QTimer.singleShot(0, self._app.quit)
                self._app.exec()
            finally:
                AppDetailsDialog.exec = orig_exec  # type: ignore[method-assign]

            self.assertEqual(opened, [rec.id])

            # Verify Add and Settings dialogs instantiate cleanly
            add_dlg = AddAppDialog(mgr, parent=win)
            add_dlg.close()
            settings_dlg = SettingsDialog(mgr, parent=win)
            settings_dlg.close()
            win.close()


if __name__ == "__main__":
    unittest.main()
