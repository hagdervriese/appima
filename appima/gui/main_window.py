"""Minimalist main window for appima — calm, Breeze-native, one action per place."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIcon, QMouseEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from appima.core.manager import AppManager
from appima.core.models import AppRecord, UpdateCheckResult
from appima.gui.add_dialog import AddAppDialog
from appima.gui.details_dialog import AppDetailsDialog
from appima.gui.settings_dialog import SettingsDialog
from appima.gui.workers import ProgressWorker, TaskWorker


class AppRowWidget(QFrame):
    """Single clean row representing one managed application."""

    clicked = Signal(str)
    update_requested = Signal(str)

    def __init__(self, record: AppRecord, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.record = record
        self.setObjectName("AppRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            """
            QFrame#AppRow {
                border: 1px solid palette(mid);
                border-radius: 10px;
                background: palette(base);
            }
            QFrame#AppRow:hover {
                border-color: palette(highlight);
            }
            """
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(14)

        # Icon
        icon_lbl = QLabel()
        icon_lbl.setFixedSize(40, 40)
        icon = (
            QIcon(record.icon_path)
            if record.icon_path and Path(record.icon_path).is_file()
            else QIcon.fromTheme("application-x-executable")
        )
        icon_lbl.setPixmap(icon.pixmap(40, 40))
        layout.addWidget(icon_lbl)

        # Name + quiet version metadata
        info = QVBoxLayout()
        info.setSpacing(2)
        name_lbl = QLabel(record.name)
        name_lbl.setStyleSheet("font-size: 14px; font-weight: 600;")
        info.addWidget(name_lbl)

        meta_parts = [f"v{record.installed_version}", record.provider]
        if record.checksum_verified:
            meta_parts.append("verified")
        if not record.is_present:
            meta_parts.append("file missing")
        meta_lbl = QLabel(" · ".join(meta_parts))
        meta_lbl.setStyleSheet("font-size: 12px; color: palette(placeholder-text);")
        info.addWidget(meta_lbl)
        layout.addLayout(info, 1)

        # Inline Update button — visible ONLY when a newer version is available
        self.update_btn: QPushButton | None = None
        if record.has_update:
            self.update_btn = QPushButton(f"Update → {record.available_version}")
            self.update_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.update_btn.setStyleSheet(
                """
                QPushButton {
                    background: palette(highlight);
                    color: palette(highlighted-text);
                    border: none;
                    border-radius: 6px;
                    padding: 6px 12px;
                    font-weight: 600;
                }
                QPushButton:hover {
                    opacity: 0.9;
                }
                """
            )
            self.update_btn.clicked.connect(
                lambda: self.update_requested.emit(self.record.id)
            )
            layout.addWidget(self.update_btn)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.record.id)
        super().mousePressEvent(event)


class MainWindow(QMainWindow):
    """Single-screen AppImage Manager with 3 header controls and zero clutter."""

    def __init__(self, manager: AppManager | None = None) -> None:
        super().__init__()
        self.manager = manager or AppManager()
        self._check_worker: TaskWorker | None = None
        self._update_worker: ProgressWorker | None = None
        self._adopt_worker: TaskWorker | None = None

        self.setWindowTitle("appima")
        self.setMinimumSize(640, 480)
        self.resize(720, 540)

        self._build_ui()
        self.refresh_apps()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(20, 18, 20, 14)
        root.setSpacing(14)

        # Minimal top bar: ONLY 3 controls (+ Add App | Check Updates · Settings)
        header = QHBoxLayout()
        header.setSpacing(10)

        self.add_btn = QPushButton("+ Add App")
        self.add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_btn.setStyleSheet(
            """
            QPushButton {
                background: palette(highlight);
                color: palette(highlighted-text);
                border: none;
                border-radius: 7px;
                padding: 7px 15px;
                font-weight: 600;
            }
            """
        )
        self.add_btn.clicked.connect(self._open_add_dialog)
        header.addWidget(self.add_btn)

        header.addStretch(1)

        self.check_btn = QPushButton("Check Updates")
        self.check_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.check_btn.clicked.connect(self._check_updates)
        header.addWidget(self.check_btn)

        self.settings_btn = QPushButton("⚙")
        self.settings_btn.setToolTip("Settings")
        self.settings_btn.setFixedWidth(36)
        self.settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_btn.clicked.connect(self._open_settings)
        header.addWidget(self.settings_btn)

        root.addLayout(header)

        # Optional banner when unmanaged AppImages are sitting in ~/Apps
        self.unmanaged_banner = QFrame()
        self.unmanaged_banner.setFrameShape(QFrame.Shape.StyledPanel)
        self.unmanaged_banner.setStyleSheet(
            "QFrame { border: 1px solid palette(mid); border-radius: 8px; background: palette(alternate-base); }"
        )
        banner_layout = QHBoxLayout(self.unmanaged_banner)
        banner_layout.setContentsMargins(12, 8, 12, 8)
        self.unmanaged_label = QLabel("")
        banner_layout.addWidget(self.unmanaged_label, 1)
        self.adopt_btn = QPushButton("Adopt")
        self.adopt_btn.clicked.connect(self._adopt_unmanaged)
        banner_layout.addWidget(self.adopt_btn)
        self.unmanaged_banner.setVisible(False)
        root.addWidget(self.unmanaged_banner)

        # Scrollable list of managed apps
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)

        self.list_container = QWidget()
        self.list_layout = QVBoxLayout(self.list_container)
        self.list_layout.setContentsMargins(0, 2, 0, 2)
        self.list_layout.setSpacing(8)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(self.list_container)
        root.addWidget(self.scroll, 1)

        # Subtle single-line footer status
        self.footer_label = QLabel("")
        self.footer_label.setStyleSheet(
            "font-size: 12px; color: palette(placeholder-text);"
        )
        root.addWidget(self.footer_label)

    def refresh_apps(self) -> None:
        # Clear existing items except trailing stretch
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

        apps = self.manager.list_apps()
        if not apps:
            empty = QLabel(
                f"No managed AppImages yet.\n\n"
                f"Click \"+ Add App\" to install one into {self.manager.config.apps_dir}."
            )
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet(
                "color: palette(placeholder-text); font-size: 13px; padding: 48px;"
            )
            self.list_layout.insertWidget(0, empty)
        else:
            for idx, rec in enumerate(apps):
                row = AppRowWidget(rec, parent=self.list_container)
                row.clicked.connect(self._open_details)
                row.update_requested.connect(self._start_update)
                self.list_layout.insertWidget(idx, row)

        # Check for unmanaged AppImages already sitting in apps_dir
        unmanaged = self.manager.scan_unmanaged()
        if unmanaged:
            noun = "AppImage" if len(unmanaged) == 1 else "AppImages"
            self.unmanaged_label.setText(
                f"{len(unmanaged)} unmanaged {noun} found in {self.manager.config.apps_dir}"
            )
            self.unmanaged_banner.setVisible(True)
        else:
            self.unmanaged_banner.setVisible(False)

        self.footer_label.setText(
            f"{len(apps)} managed · Folder: {self.manager.config.apps_dir}"
        )

    def _open_add_dialog(self) -> None:
        dialog = AddAppDialog(self.manager, parent=self)
        if dialog.exec() == AddAppDialog.DialogCode.Accepted:
            self.refresh_apps()
            if dialog.installed_record:
                self.footer_label.setText(
                    f"Installed {dialog.installed_record.name} ({dialog.installed_record.installed_version})"
                )

    def _open_details(self, app_id: str) -> None:
        record = self.manager.get_app(app_id)
        if not record:
            return
        dialog = AppDetailsDialog(self.manager, record, parent=self)
        if dialog.exec() == AppDetailsDialog.DialogCode.Accepted and dialog.changed:
            self.refresh_apps()

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self.manager, parent=self)
        if dialog.exec() == SettingsDialog.DialogCode.Accepted and dialog.changed:
            self.refresh_apps()

    def _check_updates(self) -> None:
        if self._check_worker and self._check_worker.isRunning():
            return
        self.check_btn.setEnabled(False)
        self.check_btn.setText("Checking…")
        self.footer_label.setText("Checking upstream sources for updates…")

        self._check_worker = TaskWorker(self.manager.check_all_updates, parent=self)
        self._check_worker.finished_ok.connect(self._on_check_done)
        self._check_worker.failed.connect(self._on_check_error)
        self._check_worker.start()

    def _on_check_done(self, results: list[UpdateCheckResult]) -> None:
        self.check_btn.setEnabled(True)
        self.check_btn.setText("Check Updates")
        self.refresh_apps()

        updates = [r for r in results if r.update_available]
        errors = [r for r in results if r.error]
        if updates:
            noun = "update" if len(updates) == 1 else "updates"
            self.footer_label.setText(f"{len(updates)} {noun} available.")
        elif errors:
            self.footer_label.setText(
                f"Checked with {len(errors)} warning(s): {errors[0].app_name}: {errors[0].error}"
            )
        else:
            self.footer_label.setText("All managed apps are up to date.")

    def _on_check_error(self, message: str) -> None:
        self.check_btn.setEnabled(True)
        self.check_btn.setText("Check Updates")
        self.footer_label.setText(f"Update check failed: {message}")

    def _start_update(self, app_id: str) -> None:
        if self._update_worker and self._update_worker.isRunning():
            return
        rec = self.manager.get_app(app_id)
        if not rec:
            return

        self.check_btn.setEnabled(False)
        self.add_btn.setEnabled(False)
        self.footer_label.setText(f"Updating {rec.name}…")

        self._update_worker = ProgressWorker(
            lambda cb, cancel: self.manager.update_app(
                app_id, progress_cb=cb, cancel_event=cancel
            ),
            parent=self,
        )
        self._update_worker.progress.connect(
            lambda done, total: self._on_update_progress(rec.name, done, total)
        )
        self._update_worker.finished_ok.connect(self._on_update_done)
        self._update_worker.failed.connect(self._on_update_error)
        self._update_worker.start()

    def _on_update_progress(self, name: str, done: int, total: object) -> None:
        if isinstance(total, int) and total > 0:
            pct = min(100, int(done * 100 / total))
            self.footer_label.setText(f"Updating {name}… {pct}%")
        else:
            self.footer_label.setText(
                f"Updating {name}… {done / (1024 * 1024):.1f} MiB"
            )

    def _on_update_done(self, record: AppRecord) -> None:
        self.check_btn.setEnabled(True)
        self.add_btn.setEnabled(True)
        self.refresh_apps()
        self.footer_label.setText(
            f"Updated {record.name} to version {record.installed_version}."
        )

    def _on_update_error(self, message: str) -> None:
        self.check_btn.setEnabled(True)
        self.add_btn.setEnabled(True)
        QMessageBox.critical(self, "Update failed", message)
        self.refresh_apps()

    def _adopt_unmanaged(self) -> None:
        unmanaged = self.manager.scan_unmanaged()
        if not unmanaged:
            return
        self.adopt_btn.setEnabled(False)
        self.footer_label.setText("Adopting existing AppImages…")

        def _do_adopt() -> int:
            count = 0
            for path in unmanaged:
                self.manager.install_app(str(path))
                count += 1
            return count

        self._adopt_worker = TaskWorker(_do_adopt, parent=self)
        self._adopt_worker.finished_ok.connect(self._on_adopt_done)
        self._adopt_worker.failed.connect(self._on_adopt_error)
        self._adopt_worker.start()

    def _on_adopt_done(self, count: int) -> None:
        self.adopt_btn.setEnabled(True)
        self.refresh_apps()
        self.footer_label.setText(f"Adopted {count} AppImage(s) into appima.")

    def _on_adopt_error(self, message: str) -> None:
        self.adopt_btn.setEnabled(True)
        QMessageBox.warning(self, "Adoption failed", message)
        self.refresh_apps()
