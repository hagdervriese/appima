"""Clean, progressive Add App dialog."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from appima.core.checksum import parse_checksum_input
from appima.core.manager import AppManager
from appima.core.models import AppimaError, AppRecord, DesktopConflict, ReleaseInfo
from appima.formats.appimage import _guess_name_and_version
from appima.formats.archive import _guess_archive_name_and_version, is_archive_file
from appima.gui.conflict_dialog import ResolveConflictDialog
from appima.gui.workers import ProgressWorker, TaskWorker


class AddAppDialog(QDialog):
    """Single dialog to install an AppImage from GitHub, a URL, or a local file."""

    def __init__(self, manager: AppManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.installed_record: AppRecord | None = None
        self._preview_worker: TaskWorker | None = None
        self._preflight_worker: TaskWorker | None = None
        self._install_worker: ProgressWorker | None = None

        self.setWindowTitle("Add App")
        self.setMinimumWidth(520)
        self.setModal(True)
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(16)

        heading = QLabel("Add a Portable App")
        heading.setStyleSheet("font-size: 16px; font-weight: 600;")
        root.addWidget(heading)

        subtitle = QLabel(
            "Enter a GitHub repo URL, a direct download link, or pick a local AppImage or archive (.tar.gz, .tar.xz, .zip)."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color: palette(placeholder-text);")
        root.addWidget(subtitle)

        # Source row
        source_row = QHBoxLayout()
        source_row.setSpacing(8)
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText(
            "https://github.com/owner/repo  or  /path/to/app.AppImage / .tar.gz"
        )
        self.source_edit.textChanged.connect(self._on_source_changed)
        source_row.addWidget(self.source_edit, 1)

        self.browse_btn = QPushButton("Browse…")
        self.browse_btn.clicked.connect(self._browse_file)
        source_row.addWidget(self.browse_btn)
        root.addLayout(source_row)

        # Checksum row
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)

        checksum_wrap = QVBoxLayout()
        checksum_wrap.setSpacing(4)
        self.checksum_edit = QLineEdit()
        self.checksum_edit.setPlaceholderText("Optional MD5, SHA-256, or SHA-512 hash")
        self.checksum_edit.textChanged.connect(self._on_checksum_changed)
        checksum_wrap.addWidget(self.checksum_edit)

        self.checksum_hint = QLabel("Auto-verified when GitHub publishes a checksum file.")
        self.checksum_hint.setStyleSheet(
            "font-size: 11px; color: palette(placeholder-text);"
        )
        checksum_wrap.addWidget(self.checksum_hint)
        form.addRow("Checksum", checksum_wrap)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Auto-detected from package")
        form.addRow("Name", self.name_edit)

        self.pattern_edit = QLineEdit()
        self.pattern_edit.setPlaceholderText("Optional asset filter, e.g. *linux-x64*.tar.gz")
        form.addRow("Asset filter", self.pattern_edit)
        root.addLayout(form)

        # Preview card (hidden until checked)
        self.preview_frame = QFrame()
        self.preview_frame.setFrameShape(QFrame.Shape.StyledPanel)
        self.preview_frame.setStyleSheet(
            "QFrame { border: 1px solid palette(mid); border-radius: 8px; padding: 10px; }"
        )
        preview_layout = QVBoxLayout(self.preview_frame)
        preview_layout.setContentsMargins(10, 8, 10, 8)
        preview_layout.setSpacing(3)
        self.preview_label = QLabel("")
        self.preview_label.setWordWrap(True)
        preview_layout.addWidget(self.preview_label)
        self.preview_frame.setVisible(False)
        root.addWidget(self.preview_frame)

        # Progress area (hidden until installing)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setVisible(False)
        root.addWidget(self.progress_bar)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("font-size: 12px; color: palette(placeholder-text);")
        self.status_label.setVisible(False)
        root.addWidget(self.status_label)

        # Action row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        self.check_btn = QPushButton("Inspect")
        self.check_btn.setEnabled(False)
        self.check_btn.clicked.connect(self._inspect_source)
        btn_row.addWidget(self.check_btn)

        btn_row.addStretch(1)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._on_cancel)
        btn_row.addWidget(self.cancel_btn)

        self.install_btn = QPushButton("Install")
        self.install_btn.setDefault(True)
        self.install_btn.setEnabled(False)
        self.install_btn.clicked.connect(self._start_install)
        btn_row.addWidget(self.install_btn)

        root.addLayout(btn_row)

    def _browse_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select AppImage or Portable Archive",
            str(Path.home()),
            "Portable Apps (*.AppImage *.appimage *.tar.gz *.tgz *.tar.xz *.txz *.tar.bz2 *.tbz2 *.zip);;"
            "AppImages (*.AppImage *.appimage);;"
            "Archives (*.tar.gz *.tgz *.tar.xz *.txz *.tar.bz2 *.tbz2 *.zip);;"
            "All Files (*)",
        )
        if path:
            self.source_edit.setText(path)

    def _on_source_changed(self, text: str) -> None:
        has_text = bool(text.strip())
        self.install_btn.setEnabled(has_text)
        self.check_btn.setEnabled(has_text)
        self.preview_frame.setVisible(False)

    def _on_checksum_changed(self, text: str) -> None:
        raw = text.strip()
        if not raw:
            self.checksum_hint.setText(
                "Auto-verified when GitHub publishes a checksum file."
            )
            self.checksum_hint.setStyleSheet(
                "font-size: 11px; color: palette(placeholder-text);"
            )
            return
        try:
            algo, _ = parse_checksum_input(raw)
            self.checksum_hint.setText(f"Detected {algo.upper()} checksum.")
            self.checksum_hint.setStyleSheet("font-size: 11px; color: #2ecc71;")
        except AppimaError as exc:
            self.checksum_hint.setText(str(exc))
            self.checksum_hint.setStyleSheet("font-size: 11px; color: #e74c3c;")

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.source_edit.setEnabled(not busy)
        self.browse_btn.setEnabled(not busy)
        self.checksum_edit.setEnabled(not busy)
        self.name_edit.setEnabled(not busy)
        self.pattern_edit.setEnabled(not busy)
        self.check_btn.setEnabled(not busy and bool(self.source_edit.text().strip()))
        self.install_btn.setEnabled(not busy and bool(self.source_edit.text().strip()))
        self.status_label.setVisible(bool(message))
        self.status_label.setText(message)

    def _inspect_source(self) -> None:
        source = self.source_edit.text().strip()
        pattern = self.pattern_edit.text().strip()
        if not source:
            return
        self._set_busy(True, "Inspecting source…")
        self._preview_worker = TaskWorker(
            lambda: self.manager.preview_source(source, asset_pattern=pattern),
            parent=self,
        )
        self._preview_worker.finished_ok.connect(self._on_preview_done)
        self._preview_worker.failed.connect(self._on_preview_error)
        self._preview_worker.start()

    def _on_preview_done(self, release: ReleaseInfo) -> None:
        self._set_busy(False)
        size_part = ""
        if release.size_bytes:
            size_part = f" · {release.size_bytes / (1024 * 1024):.1f} MiB"
        checksum_part = (
            f"\nChecksum: {release.checksum_algo.upper()} found automatically"
            if release.checksum
            else ""
        )
        candidate_name = self._candidate_name_from_release(release)
        conflicts = self.manager.find_conflicting_apps(candidate_name)
        conflict_part = ""
        if conflicts:
            first = conflicts[0]
            conflict_part = (
                f"\n⚠ Similar app already installed: {first.name} [{first.source_kind}]"
            )
        self.preview_label.setText(
            f"<b>{release.filename}</b>\n"
            f"Version: {release.version} · Source: {release.provider}{size_part}"
            f"{checksum_part}{conflict_part}"
        )
        self.preview_frame.setVisible(True)

    def _candidate_name_from_release(self, release: ReleaseInfo) -> str:
        if self.name_edit.text().strip():
            return self.name_edit.text().strip()
        if release.name_hint:
            return release.name_hint
        if is_archive_file(Path(release.filename)):
            guessed, _ = _guess_archive_name_and_version(release.filename)
        else:
            guessed, _ = _guess_name_and_version(release.filename)
        return guessed

    def _on_preview_error(self, message: str) -> None:
        self._set_busy(False)
        QMessageBox.warning(self, "Could not inspect source", message)

    def _start_install(self) -> None:
        source = self.source_edit.text().strip()
        checksum = self.checksum_edit.text().strip()
        pattern = self.pattern_edit.text().strip()

        if checksum:
            try:
                parse_checksum_input(checksum)
            except AppimaError as exc:
                QMessageBox.warning(self, "Invalid checksum", str(exc))
                return

        # Pre-flight: resolve candidate name and check for existing similar desktop entries
        self._set_busy(True, "Checking for existing desktop entries…")

        def _preflight() -> tuple[str, str, list[DesktopConflict]]:
            release = self.manager.preview_source(source, asset_pattern=pattern)
            candidate_name = self._candidate_name_from_release(release)
            conflicts = self.manager.find_conflicting_apps(candidate_name)
            return candidate_name, release.version, conflicts

        self._preflight_worker = TaskWorker(_preflight, parent=self)
        self._preflight_worker.finished_ok.connect(self._on_preflight_done)
        self._preflight_worker.failed.connect(self._on_install_error)
        self._preflight_worker.start()

    def _on_preflight_done(
        self, result: tuple[str, str, list[DesktopConflict]]
    ) -> None:
        candidate_name, version, conflicts = result
        if conflicts:
            dlg = ResolveConflictDialog(
                self.manager,
                candidate_name=candidate_name,
                version=version,
                conflicts=conflicts,
                parent=self,
            )
            if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.chosen_name:
                self._set_busy(False)
                return
            self.name_edit.setText(dlg.chosen_name)
        self._begin_download_and_install()

    def _begin_download_and_install(self) -> None:
        source = self.source_edit.text().strip()
        checksum = self.checksum_edit.text().strip()
        custom_name = self.name_edit.text().strip()
        pattern = self.pattern_edit.text().strip()

        self._set_busy(True, "Installing…")
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(True)

        self._install_worker = ProgressWorker(
            lambda cb, cancel: self.manager.install_app(
                source,
                expected_checksum=checksum,
                custom_name=custom_name,
                asset_pattern=pattern,
                progress_cb=cb,
                cancel_event=cancel,
            ),
            parent=self,
        )
        self._install_worker.progress.connect(self._on_progress)
        self._install_worker.finished_ok.connect(self._on_install_done)
        self._install_worker.failed.connect(self._on_install_error)
        self._install_worker.start()

    def _on_progress(self, downloaded: int, total: object) -> None:
        if isinstance(total, int) and total > 0:
            pct = min(100, int(downloaded * 100 / total))
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(pct)
            self.status_label.setText(
                f"Downloading… {downloaded / (1024 * 1024):.1f} / {total / (1024 * 1024):.1f} MiB"
            )
        else:
            self.progress_bar.setRange(0, 0)
            self.status_label.setText(
                f"Downloading… {downloaded / (1024 * 1024):.1f} MiB"
            )

    def _on_install_done(self, record: AppRecord) -> None:
        self.installed_record = record
        self.accept()

    def _on_install_error(self, message: str) -> None:
        self.progress_bar.setVisible(False)
        self._set_busy(False)
        QMessageBox.critical(self, "Installation failed", message)

    def _on_cancel(self) -> None:
        if self._install_worker and self._install_worker.isRunning():
            self._install_worker.cancel()
            self.status_label.setText("Cancelling…")
            return
        self.reject()
