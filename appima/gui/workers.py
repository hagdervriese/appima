"""Background QThread workers keeping the Qt 6 UI responsive."""

from __future__ import annotations

import threading
from typing import Any, Callable

from PySide6.QtCore import QThread, Signal


class TaskWorker(QThread):
    """Runs a blocking callable on a background thread and emits result or error."""

    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[[], Any], parent: Any = None) -> None:
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
            return
        self.finished_ok.emit(result)


class ProgressWorker(QThread):
    """Runs a cancellable download/install/update task with progress reporting."""

    progress = Signal(int, object)  # (downloaded_bytes, total_bytes | None)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        fn: Callable[[Callable[[int, int | None], None], threading.Event], Any],
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._fn = fn
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        def _emit_progress(done: int, total: int | None) -> None:
            self.progress.emit(done, total)

        try:
            result = self._fn(_emit_progress, self.cancel_event)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
            return
        self.finished_ok.emit(result)
