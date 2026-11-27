"""Read-only inspection dialog for an unmanaged AppImage or portable folder."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from appima.core.models import UnmanagedPreview


class UnmanagedDetailsDialog(QDialog):
    """Shows full metadata for an unmanaged item in ~/Apps before the user decides to adopt it."""

    def __init__(
        self,
        preview: UnmanagedPreview,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.preview = preview

        self.setWindowTitle(f"Unmanaged: {preview.name}")
        self.setMinimumWidth(500)
        self.setModal(True)
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(16)

        # Header with extracted icon + detected name/version
        header = QHBoxLayout()
        header.setSpacing(14)

        icon_lbl = QLabel()
        icon_lbl.setFixedSize(48, 48)
        icon_lbl.setPixmap(self._build_icon_pixmap())
        header.addWidget(icon_lbl)

        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        title = QLabel(self.preview.name)
        title.setStyleSheet("font-size: 17px; font-weight: 600;")
        title_col.addWidget(title)

        kind = "AppImage" if self.preview.format_id == "appimage" else "Portable folder"
        size_part = (
            f"  ·  {self.preview.size_bytes / (1024 * 1024):.1f} MiB"
            if self.preview.size_bytes
            else ""
        )
        sub = QLabel(f"Version {self.preview.version}  ·  {kind}{size_part}  ·  not integrated")
        sub.setStyleSheet("color: palette(placeholder-text); font-size: 12px;")
        title_col.addWidget(sub)
        header.addLayout(title_col, 1)
        root.addLayout(header)

        # Details card
        card = QFrame()
        card.setFrameShape(QFrame.Shape.StyledPanel)
        card.setStyleSheet(
            "QFrame { border: 1px solid palette(mid); border-radius: 8px; }"
        )
        form = QFormLayout(card)
        form.setContentsMargins(14, 12, 14, 12)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(8)

        form.addRow("Location", self._selectable_label(str(self.preview.path)))
        if self.preview.exec_path and self.preview.exec_path != str(self.preview.path):
            form.addRow("Executable", self._selectable_label(self.preview.exec_path))

        if self.preview.conflicts:
            conflict_text = "\n".join(
                f"• {c.name} [{c.source_kind}] ({c.desktop_path})"
                for c in self.preview.conflicts[:3]
            )
            lbl = self._selectable_label(conflict_text)
            lbl.setStyleSheet("color: #e67e22;")
            form.addRow("Existing match", lbl)
        else:
            ok_lbl = QLabel("No conflicting desktop entries found")
            ok_lbl.setStyleSheet("color: #2ecc71;")
            form.addRow("Desktop check", ok_lbl)

        root.addWidget(card)

        note = QLabel(
            "Adopting this app keeps it in your Apps folder and creates a managed desktop launcher entry and icon."
        )
        note.setWordWrap(True)
        note.setStyleSheet("font-size: 12px; color: palette(placeholder-text);")
        root.addWidget(note)

        # Single Close button (Adopt lives on the main list row)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)

    def _build_icon_pixmap(self) -> QPixmap:
        if self.preview.icon_bytes:
            pm = QPixmap()
            if pm.loadFromData(self.preview.icon_bytes):
                return pm.scaled(
                    48,
                    48,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
        theme_name = (
            "application-x-executable"
            if self.preview.format_id == "appimage"
            else "folder-applications"
        )
        icon = QIcon.fromTheme(theme_name, QIcon.fromTheme("application-x-executable"))
        return icon.pixmap(48, 48)

    @staticmethod
    def _selectable_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lbl.setWordWrap(True)
        return lbl
