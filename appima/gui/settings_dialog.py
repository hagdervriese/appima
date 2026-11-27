"""Minimal Settings dialog: Apps folder & background update interval."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from appima import __version__
from appima.core.manager import AppManager
from appima.core.runtime import SELF_APP_ID, current_appimage_path
from appima.service.systemd import get_timer_status, sync_update_timer

_INTERVAL_CHOICES = (
    ("off", "Off (manual only)"),
    ("6h", "Every 6 hours"),
    ("daily", "Daily"),
    ("weekly", "Weekly"),
)


class SettingsDialog(QDialog):
    """Focused settings dialog with zero clutter."""

    def __init__(self, manager: AppManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.changed = False

        self.setWindowTitle("Settings")
        self.setMinimumWidth(460)
        self.setModal(True)
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(16)

        heading = QLabel("Preferences")
        heading.setStyleSheet("font-size: 16px; font-weight: 600;")
        root.addWidget(heading)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(12)

        # Apps directory picker
        dir_row = QHBoxLayout()
        dir_row.setSpacing(8)
        self.dir_edit = QLineEdit(str(self.manager.config.apps_dir))
        dir_row.addWidget(self.dir_edit, 1)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_dir)
        dir_row.addWidget(browse_btn)
        form.addRow("Apps folder", dir_row)

        # Background update interval
        interval_wrap = QVBoxLayout()
        interval_wrap.setSpacing(4)
        self.interval_combo = QComboBox()
        for value, label in _INTERVAL_CHOICES:
            self.interval_combo.addItem(label, value)
        idx = self.interval_combo.findData(self.manager.config.check_interval)
        if idx >= 0:
            self.interval_combo.setCurrentIndex(idx)
        interval_wrap.addWidget(self.interval_combo)

        self.timer_status_label = QLabel(
            f"Background service: {get_timer_status(self.manager.paths)}"
        )
        self.timer_status_label.setStyleSheet(
            "font-size: 11px; color: palette(placeholder-text);"
        )
        interval_wrap.addWidget(self.timer_status_label)
        form.addRow("Update checks", interval_wrap)

        # Keep rollback backup toggle
        self.backup_check = QCheckBox("Keep previous version backup for one-click rollback")
        self.backup_check.setChecked(self.manager.config.keep_backup)
        form.addRow("", self.backup_check)

        root.addLayout(form)

        # Actions + subtle running version proof
        running_bin = current_appimage_path()
        version_text = f"appima v{__version__}"
        if running_bin:
            version_text += f" · {running_bin.name}"
        self.version_label = QLabel(version_text)
        self.version_label.setToolTip(
            str(running_bin) if running_bin else "Running from Python source"
        )
        self.version_label.setStyleSheet(
            "font-size: 11px; color: palette(placeholder-text);"
        )

        btn_row = QHBoxLayout()
        btn_row.addWidget(self.version_label)
        btn_row.addStretch(1)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        save_btn = QPushButton("Save")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)
        root.addLayout(btn_row)

    def _browse_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Choose Apps Folder",
            self.dir_edit.text().strip() or str(Path.home()),
        )
        if chosen:
            self.dir_edit.setText(chosen)

    def _save(self) -> None:
        raw_dir = self.dir_edit.text().strip()
        if not raw_dir:
            QMessageBox.warning(self, "Invalid folder", "Please choose an apps folder.")
            return

        interval = str(self.interval_combo.currentData())
        self.manager.update_config(
            apps_dir=Path(raw_dir).expanduser(),
            check_interval=interval,
            keep_backup=self.backup_check.isChecked(),
        )
        self_rec = self.manager.get_app(SELF_APP_ID)
        self_bin = (
            Path(self_rec.install_path)
            if self_rec and self_rec.is_present
            else None
        )
        ok, msg = sync_update_timer(
            interval, self.manager.paths, appimage_path=self_bin
        )
        if not ok:
            QMessageBox.information(self, "Background service note", msg)

        self.changed = True
        self.accept()
