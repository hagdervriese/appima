"""Minimalist main window for appima — calm, Breeze-native, one action per place."""

from __future__ import annotations

from pathlib import Path
import traceback

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
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
from appima.core.models import (
    AppimaError,
    AppRecord,
    UnmanagedPreview,
    UpdateCheckResult,
)
from appima.core.runtime import SELF_APP_ID, relaunch_appima
from appima.gui.add_dialog import AddAppDialog
from appima.gui.conflict_dialog import ResolveConflictDialog
from appima.gui.details_dialog import AppDetailsDialog
from appima.gui.settings_dialog import SettingsDialog
from appima.gui.unmanaged_dialog import UnmanagedDetailsDialog
from appima.gui.workers import ProgressWorker, TaskWorker
from appima.service.systemd import sync_update_timer


class AppRowWidget(QFrame):
    """Single clean row representing one managed application."""

    details_requested = Signal(str)
    update_requested = Signal(str)
    uninstall_requested = Signal(str)

    def __init__(self, record: AppRecord, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.record = record
        self.setObjectName("AppRow")
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
        layout.setSpacing(10)

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
                """
            )
            self.update_btn.clicked.connect(
                lambda _checked=False, aid=self.record.id: self.update_requested.emit(aid)
            )
            layout.addWidget(self.update_btn)

        # Quiet single-home actions: Details and Uninstall
        self.details_btn = QPushButton("Details", self)
        self.details_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.details_btn.setFlat(True)
        self.details_btn.setStyleSheet(
            """
            QPushButton {
                padding: 6px 10px;
                border-radius: 6px;
            }
            QPushButton:hover {
                background: palette(alternate-base);
            }
            """
        )
        self.details_btn.clicked.connect(
            lambda _checked=False, aid=self.record.id: self.details_requested.emit(aid)
        )
        layout.addWidget(self.details_btn)

        self.uninstall_btn = QPushButton("Uninstall", self)
        self.uninstall_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.uninstall_btn.setFlat(True)
        self.uninstall_btn.setStyleSheet(
            """
            QPushButton {
                padding: 6px 10px;
                border-radius: 6px;
                color: palette(placeholder-text);
            }
            QPushButton:hover {
                color: #e74c3c;
                background: palette(alternate-base);
            }
            """
        )
        self.uninstall_btn.clicked.connect(
            lambda _checked=False, aid=self.record.id: self.uninstall_requested.emit(aid)
        )
        layout.addWidget(self.uninstall_btn)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.details_requested.emit(self.record.id)
        super().mouseDoubleClickEvent(event)


class UnmanagedRowWidget(QFrame):
    """Clean row for an unmanaged AppImage or portable folder sitting in ~/Apps."""

    details_requested = Signal(str)
    adopt_requested = Signal(str)
    delete_requested = Signal(str)

    def __init__(self, path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.item_path = path
        self.setObjectName("UnmanagedRow")
        self.setStyleSheet(
            """
            QFrame#UnmanagedRow {
                border: 1px dashed palette(mid);
                border-radius: 10px;
                background: palette(alternate-base);
            }
            QFrame#UnmanagedRow:hover {
                border-color: palette(highlight);
            }
            """
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        icon_lbl = QLabel()
        icon_lbl.setFixedSize(40, 40)
        is_dir = path.is_dir()
        icon = QIcon.fromTheme(
            "folder-applications" if is_dir else "application-x-executable",
            QIcon.fromTheme("application-x-executable"),
        )
        icon_lbl.setPixmap(icon.pixmap(40, 40))
        layout.addWidget(icon_lbl)

        info = QVBoxLayout()
        info.setSpacing(2)
        name_lbl = QLabel(path.name)
        name_lbl.setStyleSheet("font-size: 14px; font-weight: 600;")
        info.addWidget(name_lbl)

        if is_dir:
            subtitle = "Portable folder · not managed yet"
        else:
            try:
                mb = path.stat().st_size / (1024 * 1024)
                subtitle = f"AppImage · {mb:.1f} MiB · not managed yet"
            except OSError:
                subtitle = "AppImage · not managed yet"
        meta_lbl = QLabel(subtitle)
        meta_lbl.setStyleSheet("font-size: 12px; color: palette(placeholder-text);")
        info.addWidget(meta_lbl)
        layout.addLayout(info, 1)

        self.details_btn = QPushButton("Details", self)
        self.details_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.details_btn.setFlat(True)
        self.details_btn.setStyleSheet(
            """
            QPushButton {
                padding: 6px 10px;
                border-radius: 6px;
            }
            QPushButton:hover {
                background: palette(base);
            }
            """
        )
        self.details_btn.clicked.connect(
            lambda _checked=False, p=str(path): self.details_requested.emit(p)
        )
        layout.addWidget(self.details_btn)

        self.adopt_btn = QPushButton("Adopt", self)
        self.adopt_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.adopt_btn.setStyleSheet(
            """
            QPushButton {
                border: 1px solid palette(highlight);
                border-radius: 6px;
                padding: 5px 12px;
                font-weight: 600;
            }
            QPushButton:hover {
                background: palette(highlight);
                color: palette(highlighted-text);
            }
            """
        )
        self.adopt_btn.clicked.connect(
            lambda _checked=False, p=str(path): self.adopt_requested.emit(p)
        )
        layout.addWidget(self.adopt_btn)

        self.delete_btn = QPushButton("Delete", self)
        self.delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete_btn.setFlat(True)
        self.delete_btn.setStyleSheet(
            """
            QPushButton {
                padding: 6px 10px;
                border-radius: 6px;
                color: palette(placeholder-text);
            }
            QPushButton:hover {
                color: #e74c3c;
                background: palette(base);
            }
            """
        )
        self.delete_btn.clicked.connect(
            lambda _checked=False, p=str(path): self.delete_requested.emit(p)
        )
        layout.addWidget(self.delete_btn)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.details_requested.emit(str(self.item_path))
        super().mouseDoubleClickEvent(event)


class MainWindow(QMainWindow):
    """Single-screen AppImage Manager with 3 header controls and zero clutter."""

    def __init__(self, manager: AppManager | None = None) -> None:
        super().__init__()
        self.manager = manager or AppManager()
        self._check_worker: TaskWorker | None = None
        self._update_worker: ProgressWorker | None = None
        self._adopt_worker: TaskWorker | None = None
        self._inspect_worker: TaskWorker | None = None
        self._self_integrate_worker: TaskWorker | None = None

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

        # Optional banner when running appima as an unintegrated standalone AppImage
        self.self_banner = QFrame()
        self.self_banner.setFrameShape(QFrame.Shape.StyledPanel)
        self.self_banner.setStyleSheet(
            "QFrame { border: 1px solid palette(highlight); border-radius: 8px; background: palette(alternate-base); }"
        )
        self_banner_layout = QHBoxLayout(self.self_banner)
        self_banner_layout.setContentsMargins(12, 8, 12, 8)
        self.self_banner_label = QLabel("")
        self_banner_layout.addWidget(self.self_banner_label, 1)
        self.integrate_self_btn = QPushButton("Integrate appima")
        self.integrate_self_btn.clicked.connect(self._integrate_self)
        self_banner_layout.addWidget(self.integrate_self_btn)
        self.self_banner.setVisible(False)
        root.addWidget(self.self_banner)

        # Scrollable list of managed (and any unmanaged) apps
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
        unmanaged = self.manager.scan_unmanaged()
        insert_idx = 0

        if not apps and not unmanaged:
            empty = QLabel(
                f"No managed apps yet.\n\n"
                f"Click \"+ Add App\" to install an AppImage or portable archive into {self.manager.config.apps_dir}."
            )
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet(
                "color: palette(placeholder-text); font-size: 13px; padding: 48px;"
            )
            self.list_layout.insertWidget(0, empty)
        else:
            for rec in apps:
                row = AppRowWidget(rec, parent=self.list_container)
                row.details_requested.connect(self._open_details)
                row.update_requested.connect(self._start_update)
                row.uninstall_requested.connect(self._confirm_uninstall)
                self.list_layout.insertWidget(insert_idx, row)
                insert_idx += 1

            if unmanaged:
                section_lbl = QLabel(
                    f"Unmanaged in {self.manager.config.apps_dir.name} ({len(unmanaged)})"
                )
                section_lbl.setStyleSheet(
                    "font-size: 11px; font-weight: 600; color: palette(placeholder-text); "
                    "padding-top: 10px; padding-left: 2px;"
                )
                self.list_layout.insertWidget(insert_idx, section_lbl)
                insert_idx += 1

                for path in unmanaged:
                    u_row = UnmanagedRowWidget(path, parent=self.list_container)
                    u_row.details_requested.connect(self._inspect_unmanaged_item)
                    u_row.adopt_requested.connect(self._adopt_single_unmanaged)
                    u_row.delete_requested.connect(self._confirm_delete_unmanaged)
                    self.list_layout.insertWidget(insert_idx, u_row)
                    insert_idx += 1

        # Offer one-click self-integration when running as an unintegrated AppImage
        if self.manager.needs_self_integration():
            self.self_banner_label.setText(
                f"Integrate appima into {self.manager.config.apps_dir} and your Application Menu?"
            )
            self.self_banner.setVisible(True)
        else:
            self.self_banner.setVisible(False)

        unmanaged_part = f" · {len(unmanaged)} unmanaged" if unmanaged else ""
        self.footer_label.setText(
            f"{len(apps)} managed{unmanaged_part} · Folder: {self.manager.config.apps_dir}"
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
        try:
            record = self.manager.get_app(app_id)
            if not record:
                return
            dialog = AppDetailsDialog(self.manager, record, parent=self)
            result = dialog.exec()
            if result == QDialog.DialogCode.Accepted and dialog.changed:
                self.refresh_apps()
            elif dialog.changed:
                self.refresh_apps()
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            QMessageBox.critical(
                self,
                "Could not open Details",
                f"An unexpected error occurred while opening details:\n{exc}",
            )

    def _confirm_uninstall(self, app_id: str) -> None:
        record = self.manager.get_app(app_id)
        if not record:
            return
        reply = QMessageBox.question(
            self,
            "Uninstall app",
            f"Remove {record.name}?\n\n"
            f"This deletes:\n"
            f"• {record.install_path}\n"
            f"• Its desktop launcher entry and icon",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self.manager.uninstall_app(app_id)
        except AppimaError as exc:
            QMessageBox.critical(self, "Uninstall failed", str(exc))
            return
        self.refresh_apps()
        self.footer_label.setText(f"Uninstalled {record.name}.")

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
        is_self = app_id == SELF_APP_ID
        if is_self:
            self.footer_label.setText(
                f"Updating {rec.name}… (appima will restart automatically after update)"
            )
        else:
            self.footer_label.setText(f"Updating {rec.name}…")

        self._update_worker = ProgressWorker(
            lambda cb, cancel: self.manager.update_app(
                app_id, progress_cb=cb, cancel_event=cancel
            ),
            parent=self,
        )
        self._update_worker.progress.connect(
            lambda done, total: self._on_update_progress(
                rec.name, done, total, is_self=is_self
            )
        )
        self._update_worker.finished_ok.connect(self._on_update_done)
        self._update_worker.failed.connect(self._on_update_error)
        self._update_worker.start()

    def _on_update_progress(
        self, name: str, done: int, total: object, is_self: bool = False
    ) -> None:
        suffix = " — will restart after update" if is_self else ""
        if isinstance(total, int) and total > 0:
            pct = min(100, int(done * 100 / total))
            self.footer_label.setText(f"Updating {name}… {pct}%{suffix}")
        else:
            self.footer_label.setText(
                f"Updating {name}… {done / (1024 * 1024):.1f} MiB{suffix}"
            )

    def _on_update_done(self, record: AppRecord) -> None:
        self.check_btn.setEnabled(True)
        self.add_btn.setEnabled(True)
        self.refresh_apps()
        if record.id == SELF_APP_ID:
            if self.manager.config.check_interval != "off":
                sync_update_timer(
                    self.manager.config.check_interval,
                    self.manager.paths,
                    appimage_path=Path(record.install_path),
                )
            self.footer_label.setText(
                f"Updated appima to v{record.installed_version} — restarting automatically…"
            )
            QTimer.singleShot(
                1200, lambda path=record.install_path: self._restart_self(path)
            )
        else:
            self.footer_label.setText(
                f"Updated {record.name} to version {record.installed_version}."
            )

    def _restart_self(self, binary_path: str) -> None:
        try:
            relaunch_appima(binary_path)
            QApplication.quit()
        except OSError as exc:
            self.footer_label.setText(
                f"Updated appima — please restart manually ({exc})."
            )

    def _integrate_self(self) -> None:
        if self._self_integrate_worker and self._self_integrate_worker.isRunning():
            return
        self.integrate_self_btn.setEnabled(False)
        self.footer_label.setText("Integrating appima into your system…")

        def _do_integrate() -> AppRecord:
            rec = self.manager.integrate_self()
            if self.manager.config.check_interval != "off":
                sync_update_timer(
                    self.manager.config.check_interval,
                    self.manager.paths,
                    appimage_path=Path(rec.install_path),
                )
            return rec

        self._self_integrate_worker = TaskWorker(_do_integrate, parent=self)
        self._self_integrate_worker.finished_ok.connect(self._on_self_integrate_done)
        self._self_integrate_worker.failed.connect(self._on_self_integrate_error)
        self._self_integrate_worker.start()

    def _on_self_integrate_done(self, record: AppRecord) -> None:
        self.integrate_self_btn.setEnabled(True)
        self.refresh_apps()
        self.footer_label.setText(
            f"Integrated appima into {record.install_path} and your Application Menu."
        )

    def _on_self_integrate_error(self, message: str) -> None:
        self.integrate_self_btn.setEnabled(True)
        QMessageBox.warning(self, "Self-integration failed", message)
        self.refresh_apps()

    def _on_update_error(self, message: str) -> None:
        self.check_btn.setEnabled(True)
        self.add_btn.setEnabled(True)
        QMessageBox.critical(self, "Update failed", message)
        self.refresh_apps()

    def _inspect_unmanaged_item(self, raw_path: str) -> None:
        if self._inspect_worker and self._inspect_worker.isRunning():
            return
        self.footer_label.setText(f"Inspecting {Path(raw_path).name}…")
        self._inspect_worker = TaskWorker(
            lambda p=raw_path: self.manager.inspect_unmanaged(p), parent=self
        )
        self._inspect_worker.finished_ok.connect(self._on_inspect_unmanaged_done)
        self._inspect_worker.failed.connect(self._on_inspect_unmanaged_error)
        self._inspect_worker.start()

    def _on_inspect_unmanaged_done(self, preview: UnmanagedPreview) -> None:
        try:
            dlg = UnmanagedDetailsDialog(preview, parent=self)
            dlg.exec()
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            QMessageBox.critical(
                self, "Could not inspect app", f"Unexpected error:\n{exc}"
            )
        finally:
            self.refresh_apps()

    def _on_inspect_unmanaged_error(self, message: str) -> None:
        QMessageBox.warning(self, "Could not inspect app", message)
        self.refresh_apps()

    def _adopt_single_unmanaged(self, raw_path: str) -> None:
        if self._adopt_worker and self._adopt_worker.isRunning():
            return
        path = Path(raw_path)
        self.footer_label.setText(f"Inspecting {path.name}…")
        try:
            preview = self.manager.inspect_unmanaged(path)
        except AppimaError as exc:
            QMessageBox.warning(self, "Could not inspect app", str(exc))
            self.refresh_apps()
            return

        custom_name = ""
        if preview.conflicts:
            dlg = ResolveConflictDialog(
                self.manager,
                candidate_name=preview.name,
                version=preview.version,
                conflicts=preview.conflicts,
                parent=self,
            )
            if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.chosen_name:
                self.refresh_apps()
                return
            custom_name = dlg.chosen_name

        self.footer_label.setText(f"Adopting {custom_name or preview.name}…")
        self._adopt_worker = TaskWorker(
            lambda p=str(path), n=custom_name: self.manager.install_app(
                p, custom_name=n
            ),
            parent=self,
        )
        self._adopt_worker.finished_ok.connect(self._on_adopt_done)
        self._adopt_worker.failed.connect(self._on_adopt_error)
        self._adopt_worker.start()

    def _on_adopt_done(self, record: AppRecord) -> None:
        self.refresh_apps()
        self.footer_label.setText(
            f"Adopted {record.name} ({record.installed_version}) into appima."
        )

    def _on_adopt_error(self, message: str) -> None:
        QMessageBox.warning(self, "Adoption failed", message)
        self.refresh_apps()

    def _confirm_delete_unmanaged(self, raw_path: str) -> None:
        path = Path(raw_path)
        kind = "folder" if path.is_dir() else "file"
        reply = QMessageBox.question(
            self,
            "Delete unmanaged app",
            f"Permanently delete unmanaged {kind} \"{path.name}\"?\n\n• {path}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self.manager.delete_unmanaged(path)
        except AppimaError as exc:
            QMessageBox.critical(self, "Delete failed", str(exc))
            return
        self.refresh_apps()
        self.footer_label.setText(f"Deleted {path.name}.")
