"""Dialog enforcing a unique desktop launcher name or removing obsolete conflicting apps."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
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
from appima.core.models import AppimaError, DesktopConflict


class ResolveConflictDialog(QDialog):
    """Warns about existing similar apps, offers safe removal of obsolete ones,
    and requires a unique launcher name if conflicts remain.
    """

    def __init__(
        self,
        manager: AppManager,
        candidate_name: str,
        version: str,
        conflicts: list[DesktopConflict],
        reserved_names: set[str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.manager = manager
        self.candidate_name = candidate_name
        self.version = version
        self.conflicts = list(conflicts)
        self.reserved_names = {n.strip().lower() for n in (reserved_names or set())}
        self.chosen_name = ""
        self.removed_any = False

        self.setWindowTitle("Similar application already exists")
        self.setMinimumWidth(520)
        self.setModal(True)

        self._build_ui()
        self._refresh_conflict_rows()
        self._validate_name(self.name_edit.text())

    def _initial_suggestion(self, base_name: str, version: str) -> str:
        if not self.conflicts and base_name.strip().lower() not in self.reserved_names:
            return base_name.strip()
        suggestion = self.manager.suggest_unique_name(base_name, version)
        idx = 2
        while suggestion.strip().lower() in self.reserved_names:
            suggestion = f"{base_name.strip()} ({idx})"
            idx += 1
        return suggestion

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(14)

        heading = QLabel("Similar desktop entry already exists")
        heading.setStyleSheet("font-size: 15px; font-weight: 600;")
        root.addWidget(heading)

        self.intro_label = QLabel(
            f"An application matching <b>{self.candidate_name}</b> is already registered on your system. "
            "You can remove an obsolete entry below, choose a unique launcher name to keep both side-by-side, or cancel."
        )
        self.intro_label.setWordWrap(True)
        root.addWidget(self.intro_label)

        # Dynamic list of conflicting entries (with inline Remove… button when safely removable)
        self.card = QFrame()
        self.card.setFrameShape(QFrame.Shape.StyledPanel)
        self.card.setStyleSheet(
            "QFrame { border: 1px solid palette(mid); border-radius: 8px; background: palette(alternate-base); }"
        )
        self.card_layout = QVBoxLayout(self.card)
        self.card_layout.setContentsMargins(12, 10, 12, 10)
        self.card_layout.setSpacing(8)
        root.addWidget(self.card)

        # Unique name input + live validation
        prompt_lbl = QLabel("Launcher name for the new installation:")
        prompt_lbl.setStyleSheet("font-weight: 600;")
        root.addWidget(prompt_lbl)

        self.name_edit = QLineEdit(
            self._initial_suggestion(self.candidate_name, self.version)
        )
        self.name_edit.selectAll()
        self.name_edit.textChanged.connect(self._validate_name)
        root.addWidget(self.name_edit)

        self.hint_label = QLabel("")
        self.hint_label.setWordWrap(True)
        self.hint_label.setStyleSheet("font-size: 11px;")
        root.addWidget(self.hint_label)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        self.continue_btn = QPushButton("Continue")
        self.continue_btn.setDefault(True)
        self.continue_btn.clicked.connect(self._accept_unique)
        btn_row.addWidget(self.continue_btn)
        root.addLayout(btn_row)

    def _refresh_conflict_rows(self) -> None:
        while self.card_layout.count():
            item = self.card_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        if not self.conflicts:
            cleared = QLabel(
                "All conflicting entries have been removed. You can install under the original name."
            )
            cleared.setWordWrap(True)
            cleared.setStyleSheet("color: #2ecc71; font-size: 12px;")
            self.card_layout.addWidget(cleared)
            return

        for conflict in self.conflicts[:5]:
            row_widget = QWidget()
            row = QHBoxLayout(row_widget)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(10)

            info = QLabel(
                f"<b>{conflict.name}</b> [{conflict.source_kind}]<br>"
                f"<span style='font-size:11px; color:palette(placeholder-text);'>{conflict.desktop_path}</span>"
            )
            info.setWordWrap(True)
            row.addWidget(info, 1)

            plan = self.manager.plan_conflict_removal(conflict)
            if plan.can_remove:
                rm_btn = QPushButton("Remove…", row_widget)
                rm_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                rm_btn.setStyleSheet("color: #e74c3c;")
                rm_btn.clicked.connect(
                    lambda _checked=False, c=conflict: self._prompt_remove_conflict(c)
                )
                row.addWidget(rm_btn)
            else:
                locked = QLabel("OS-managed")
                locked.setToolTip(plan.reason)
                locked.setStyleSheet(
                    "font-size: 11px; color: palette(placeholder-text);"
                )
                row.addWidget(locked)

            self.card_layout.addWidget(row_widget)

        if len(self.conflicts) > 5:
            more = QLabel(f"• and {len(self.conflicts) - 5} more…")
            more.setStyleSheet("font-size: 11px; color: palette(placeholder-text);")
            self.card_layout.addWidget(more)

    def _prompt_remove_conflict(self, conflict: DesktopConflict) -> None:
        plan = self.manager.plan_conflict_removal(conflict)
        if not plan.can_remove:
            QMessageBox.information(self, "Cannot remove entry", plan.reason)
            return

        details_lines: list[str] = [plan.summary]
        if plan.method == "local_files" and plan.paths_to_delete:
            details_lines.append("\nThe following paths will be permanently deleted:")
            for p in plan.paths_to_delete:
                kind = "folder" if p.is_dir() else "file"
                details_lines.append(f"• ({kind}) {p}")
        elif plan.method == "flatpak":
            details_lines.append(
                f"\nRuns: flatpak uninstall {plan.flatpak_scope} -y {plan.flatpak_id}"
            )
        elif plan.method == "appima":
            details_lines.append(
                "\nRemoves the managed app binary/folder, desktop entry, and icon."
            )

        reply = QMessageBox.question(
            self,
            "Remove obsolete application",
            "\n".join(details_lines) + "\n\nProceed?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            self.manager.remove_conflict(conflict)
        except AppimaError as exc:
            QMessageBox.critical(self, "Removal failed", str(exc))
            return

        self.removed_any = True
        self.conflicts = self.manager.find_conflicting_apps(self.candidate_name)
        self._refresh_conflict_rows()
        if not self.conflicts:
            self.name_edit.setText(self.candidate_name)
        self._validate_name(self.name_edit.text())

    def _validate_name(self, text: str) -> None:
        cleaned = text.strip()
        if not cleaned:
            self.hint_label.setText("Please enter a unique application name.")
            self.hint_label.setStyleSheet("font-size: 11px; color: #e74c3c;")
            self.continue_btn.setEnabled(False)
            return

        existing = self.manager.has_exact_name_conflict(cleaned)
        if existing is not None or cleaned.lower() in self.reserved_names:
            owner = f" ({existing.source_kind})" if existing else ""
            self.hint_label.setText(
                f"\"{cleaned}\" is already used by an existing desktop entry{owner}. "
                "Remove it above or choose a unique name (e.g. include the version)."
            )
            self.hint_label.setStyleSheet("font-size: 11px; color: #e74c3c;")
            self.continue_btn.setEnabled(False)
            return

        self.hint_label.setText("Unique launcher name — ready to install.")
        self.hint_label.setStyleSheet("font-size: 11px; color: #2ecc71;")
        self.continue_btn.setEnabled(True)

    def _accept_unique(self) -> None:
        cleaned = self.name_edit.text().strip()
        if not cleaned or self.manager.has_exact_name_conflict(cleaned) is not None:
            self._validate_name(cleaned)
            return
        self.chosen_name = cleaned
        self.accept()
