"""Single, uncluttered App Details dialog — one home for every per-app action."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
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
        self._pending_icon_file: Path | None = None
        self._reextract_icon = False

        self.setWindowTitle(record.name)
        self.setMinimumWidth(520)
        self.setModal(True)
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(16)

        # Header with icon + name + version + icon controls
        header = QHBoxLayout()
        header.setSpacing(14)

        self.icon_label = QLabel()
        self.icon_label.setFixedSize(48, 48)
        self._set_preview_icon(self._load_icon())
        header.addWidget(self.icon_label)

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

        icon_btn_col = QVBoxLayout()
        icon_btn_col.setSpacing(4)
        change_icon_btn = QPushButton("Change Icon…")
        change_icon_btn.clicked.connect(self._choose_icon)
        icon_btn_col.addWidget(change_icon_btn)

        reextract_btn = QPushButton("Re-extract")
        reextract_btn.setToolTip("Re-read the icon embedded inside the AppImage")
        reextract_btn.clicked.connect(self._reextract_embedded_icon)
        icon_btn_col.addWidget(reextract_btn)
        header.addLayout(icon_btn_col)
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

        self.exec_edit: QLineEdit | None = None
        if self.record.format == "archive":
            exec_row = QHBoxLayout()
            exec_row.setSpacing(6)
            self.exec_edit = QLineEdit(self.record.effective_exec_path)
            self.exec_edit.setPlaceholderText("Path to launch executable inside app folder")
            exec_row.addWidget(self.exec_edit, 1)
            browse_exec_btn = QPushButton("Browse…")
            browse_exec_btn.clicked.connect(self._choose_executable)
            exec_row.addWidget(browse_exec_btn)
            edit_form.addRow("Executable", exec_row)

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
        divider.setFrameShadow(QFrame.Shadow.Sunken)
        root.addWidget(divider)

        actions = QHBoxLayout()
        actions.setSpacing(10)

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

    def _set_preview_icon(self, icon: QIcon) -> None:
        self.icon_label.setPixmap(icon.pixmap(48, 48))

    def _choose_icon(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose Application Icon",
            str(Path.home()),
            "Images (*.svg *.png *.ico *.xpm *.webp);;All Files (*)",
        )
        if path:
            self._pending_icon_file = Path(path)
            self._reextract_icon = False
            self._set_preview_icon(QIcon(path))

    def _reextract_embedded_icon(self) -> None:
        try:
            self.record = self.manager.reintegrate_app(
                self.record.id, reextract_icon=True
            )
        except AppimaError as exc:
            QMessageBox.warning(self, "Could not re-extract icon", str(exc))
            return
        self._pending_icon_file = None
        self._reextract_icon = False
        self.changed = True
        self._set_preview_icon(self._load_icon())

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

    def _choose_executable(self) -> None:
        if not self.exec_edit:
            return
        start_dir = (
            self.record.install_path
            if Path(self.record.install_path).is_dir()
            else str(Path.home())
        )
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Launch Executable",
            start_dir,
            "All Files (*)",
        )
        if path:
            self.exec_edit.setText(path)

    def _save(self) -> None:
        try:
            self.record = self.manager.update_app_settings(
                self.record.id,
                name=self.name_edit.text(),
                source_url=self.source_edit.text(),
                launch_args=self.args_edit.text(),
                env_vars=self.env_edit.text(),
                exec_path=self.exec_edit.text() if self.exec_edit else None,
                custom_icon_file=self._pending_icon_file,
                reextract_icon=self._reextract_icon,
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
