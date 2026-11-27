"""Dialog enforcing a unique desktop launcher name when a similar app already exists."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from appima.core.manager import AppManager
from appima.core.models import DesktopConflict


class ResolveConflictDialog(QDialog):
    """Warns about existing similar apps and requires a unique name to continue."""

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
        self.conflicts = conflicts
        self.reserved_names = {n.strip().lower() for n in (reserved_names or set())}
        self.chosen_name = ""

        self.setWindowTitle("Similar application already exists")
        self.setMinimumWidth(480)
        self.setModal(True)

        suggested = self._initial_suggestion(candidate_name, version)
        self._build_ui(candidate_name, suggested)
        self._validate_name(self.name_edit.text())

    def _initial_suggestion(self, base_name: str, version: str) -> str:
        # If the candidate name itself is not an exact collision, still offer
        # a versioned suggestion when an exact match exists, else keep candidate.
        if (
            self.manager.has_exact_name_conflict(base_name) is not None
            or base_name.strip().lower() in self.reserved_names
        ):
            suggestion = self.manager.suggest_unique_name(base_name, version)
            idx = 2
            while suggestion.strip().lower() in self.reserved_names:
                suggestion = f"{base_name.strip()} ({idx})"
                idx += 1
            return suggestion
        return self.manager.suggest_unique_name(base_name, version)

    def _build_ui(self, candidate_name: str, suggested_name: str) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(14)

        heading = QLabel("Similar desktop entry already exists")
        heading.setStyleSheet("font-size: 15px; font-weight: 600;")
        root.addWidget(heading)

        intro = QLabel(
            f"An application matching <b>{candidate_name}</b> is already registered on your system. "
            "To keep both versions clearly distinguishable in your Application Menu, "
            "enter a unique launcher name below or cancel."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        # Conflict list card
        card = QFrame()
        card.setFrameShape(QFrame.Shape.StyledPanel)
        card.setStyleSheet(
            "QFrame { border: 1px solid palette(mid); border-radius: 8px; background: palette(alternate-base); }"
        )
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(12, 10, 12, 10)
        card_layout.setSpacing(4)
        lines = [
            f"• <b>{c.name}</b> [{c.source_kind}]<br><span style='font-size:11px; color:palette(placeholder-text);'>{c.desktop_path}</span>"
            for c in self.conflicts[:4]
        ]
        if len(self.conflicts) > 4:
            lines.append(f"• and {len(self.conflicts) - 4} more…")
        list_lbl = QLabel("<br>".join(lines))
        list_lbl.setWordWrap(True)
        card_layout.addWidget(list_lbl)
        root.addWidget(card)

        # Unique name input + live validation
        prompt_lbl = QLabel("Unique launcher name:")
        prompt_lbl.setStyleSheet("font-weight: 600;")
        root.addWidget(prompt_lbl)

        self.name_edit = QLineEdit(suggested_name)
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

        self.continue_btn = QPushButton("Continue with Unique Name")
        self.continue_btn.setDefault(True)
        self.continue_btn.clicked.connect(self._accept_unique)
        btn_row.addWidget(self.continue_btn)
        root.addLayout(btn_row)

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
                "Please choose a unique name (e.g. include the version)."
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
