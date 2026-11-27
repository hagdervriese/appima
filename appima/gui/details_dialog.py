"""Single, uncluttered App Details dialog — one home for every per-app action."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from appima.core.manager import AppManager
from appima.core.models import AppimaError, AppRecord


class AppDetailsDialog(QDialog):
    """View metadata, edit launch options, roll back, or uninstall an app."""

    def __init__(
        self,
        manager: AppManager,
        record: AppRecord,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.manager = manager
        self.record = record
        self.changed = False

        self.setWindowTitle(record.name)
        self.setMinimumWidth(520)
        self.setModal(True)
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(16)

        # Header with icon + name + version
        header = QHBoxLayout()
        header.setSpacing(14)

        icon_label = QLabel()
        icon_label.setFixedSize(48, 48)
        icon = self._load_icon()
        icon_label.setPixmap(icon.pixmap(48, 48))
        header.addWidget(icon_label)

        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        title = QLabel(self.record.name)
        title.setStyleSheet("font-size: 17px; font-weight: 600;")
        title_col.addWidget(title)

        sub = QLabel(
            f"Version {self.record.installed_version}  ·  {self.record.format}  ·  {self.record.provider}"
        )
        sub.setStyleSheet("color: palette(placeholder-text); font-size: 12px;")
        title_col.addWidget(sub)
        header.addLayout(title_col, 1)
        root.addLayout(header)

        # Read-only info card
        info_frame = QFrame()
        info_frame.setFrameShape(QFrame.Shape.StyledPanel)
        info_frame.setStyleSheet(
            "QFrame { border: 1px solid palette(mid); border-radius: 8px; }"
        )
        info_form = QFormLayout(info_frame)
        info_form.setContentsMargins(14, 12, 14, 12)
        info_form.setHorizontalSpacing(14)
        info_form.setVerticalSpacing(8)

        info_form.addRow("Location", self._selectable_label(self.record.install_path))
        info_form.addRow(
            "Desktop entry", self._selectable_label(self.record.desktop_file or "—")
        )

        checksum_text = "—"
        if self.record.checksum_value:
            short_hash = f"{self.record.checksum_value[:16]}…"
            badge = "verified" if self.record.checksum_verified else "recorded"
            checksum_text = f"{self.record.checksum_algo.upper()} ({badge}) · {short_hash}"
        info_form.addRow("Integrity", self._selectable_label(checksum_text))
        root.addWidget(info_frame)

        # Editable integration settings
        edit_form = QFormLayout()
        edit_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        edit_form.setHorizontalSpacing(14)
        edit_form.setVerticalSpacing(10)

        self.name_edit = QLineEdit(self.record.name)
        edit_form.addRow("Display name", self.name_edit)

        self.source_edit = QLineEdit(self.record.source_url)
        self.source_edit.setPlaceholderText("GitHub repo URL or direct download URL")
        edit_form.addRow("Update source", self.source_edit)

        self.args_edit = QLineEdit(self.record.launch_args)
        self.args_edit.setPlaceholderText("Optional flags, e.g. --no-sandbox")
        edit_form.addRow("Launch args", self.args_edit)

        self.env_edit = QLineEdit(self.record.env_vars)
        self.env_edit.setPlaceholderText("Optional env vars, e.g. ELECTRON_OZONE_PLATFORM_HINT=auto")
        edit_form.addRow("Environment", self.env_edit)
        root.addLayout(edit_form)

        # Bottom single-home action bar
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setFrameShadow(QFrame.Shape.Sunken)
        root.addWidget(divider)

        actions = QHBoxLayout()
        actions.setSpacing(10)

        self.uninstall_btn = QPushButton("Uninstall")
        self.uninstall_btn.setStyleSheet("color: #e74c3c;")
        self.uninstall_btn.clicked.connect(self._uninstall)
        actions.addWidget(self.uninstall_btn)

        if self.record.has_backup:
            rb_label = (
                f"Rollback to {self.record.backup_version}"
                if self.record.backup_version
                else "Rollback"
            )
            self.rollback_btn = QPushButton(rb_label)
            self.rollback_btn.clicked.connect(self._rollback)
            actions.addWidget(self.rollback_btn)

        actions.addStretch(1)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        actions.addWidget(close_btn)

        save_btn = QPushButton("Save")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._save)
        actions.addWidget(save_btn)

        root.addLayout(actions)

    def _load_icon(self) -> QIcon:
        if self.record.icon_path and Path(self.record.icon_path).is_file():
            return QIcon(self.record.icon_path)
        return QIcon.fromTheme("application-x-executable")

    @staticmethod
    def _selectable_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lbl.setWordWrap(True)
        return lbl

    def _save(self) -> None:
        try:
            self.record = self.manager.update_app_settings(
                self.record.id,
                name=self.name_edit.text(),
                source_url=self.source_edit.text(),
                launch_args=self.args_edit.text(),
                env_vars=self.env_edit.text(),
            )
        except AppimaError as exc:
            QMessageBox.warning(self, "Could not save changes", str(exc))
            return
        self.changed = True
        self.accept()

    def _rollback(self) -> None:
        reply = QMessageBox.question(
            self,
            "Rollback app",
            f"Restore {self.record.name} to version {self.record.backup_version or 'previous'}?",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self.record = self.manager.rollback_app(self.record.id)
        except AppimaError as exc:
            QMessageBox.critical(self, "Rollback failed", str(exc))
            return
        self.changed = True
        self.accept()

    def _uninstall(self) -> None:
        reply = QMessageBox.question(
            self,
            "Uninstall app",
            f"Remove {self.record.name}, its file in {Path(self.record.install_path).parent}, "
            "and its desktop entry?",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self.manager.uninstall_app(self.record.id)
        except AppimaError as exc:
            QMessageBox.critical(self, "Uninstall failed", str(exc))
            return
        self.changed = True
        self.accept()
