from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot

from services.runtime.WhisperModelInstallWorker import WhisperModelInstallWorker


logger = logging.getLogger(__name__)


class SubtitleWhisperModelFlow(QObject):
    status_changed = Signal(int, str)
    details_changed = Signal(int, str)
    finished = Signal(int)
    failed = Signal(int, str)
    canceled = Signal(int)
    thread_finished = Signal(int)

    def __init__(self, parent: QObject):
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: WhisperModelInstallWorker | None = None
        self._cancel_requested = False
        self._run_id: int | None = None

    def start(self, run_id: int, model_size: str) -> bool:
        if self.is_active():
            logger.warning(
                "Rejected Whisper model flow start because a previous install flow is active | active_run_id=%s | new_run_id=%s",
                self._run_id,
                run_id,
            )
            return False

        thread = QThread(self.parent())
        worker = WhisperModelInstallWorker(model_size)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.status_changed.connect(self._on_worker_status_changed, Qt.QueuedConnection)
        worker.details_changed.connect(self._on_worker_details_changed, Qt.QueuedConnection)
        worker.finished.connect(self._on_worker_finished, Qt.QueuedConnection)
        worker.failed.connect(self._on_worker_failed, Qt.QueuedConnection)
        worker.canceled.connect(self._on_worker_canceled, Qt.QueuedConnection)

        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.canceled.connect(thread.quit)
        thread.finished.connect(lambda run_id=run_id, thread=thread: self._on_thread_finished(run_id, thread))
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        self._thread = thread
        self._worker = worker
        self._cancel_requested = False
        self._run_id = run_id
        QTimer.singleShot(0, lambda run_id=run_id, thread=thread: self._deferred_start(run_id, thread))
        return True

    def request_stop(self, *, force: bool) -> bool:
        if self._worker is None:
            return False
        if force:
            self._worker.force_stop()
            return True
        if self._cancel_requested:
            return False
        self._cancel_requested = True
        self._worker.cancel()
        return True

    def is_active(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def _deferred_start(self, run_id: int, thread: QThread):
        if self._run_id != run_id or self._thread is not thread or self._worker is None:
            return
        if not thread.isRunning():
            thread.start()

    def _emit_active_worker_event(self, event_name: str, signal, *args):
        if self.sender() is not self._worker:
            logger.debug("Ignoring %s from stale Whisper model worker", event_name)
            return
        run_id = self._run_id
        if run_id is not None:
            signal.emit(run_id, *args)

    @Slot(str)
    def _on_worker_status_changed(self, text: str):
        self._emit_active_worker_event("Whisper model status update", self.status_changed, text)

    @Slot(str)
    def _on_worker_details_changed(self, text: str):
        self._emit_active_worker_event("Whisper model details update", self.details_changed, text)

    @Slot()
    def _on_worker_finished(self):
        self._emit_active_worker_event("Whisper model finished", self.finished)

    @Slot(str)
    def _on_worker_failed(self, error_text: str):
        self._emit_active_worker_event("Whisper model failed", self.failed, error_text)

    @Slot()
    def _on_worker_canceled(self):
        self._emit_active_worker_event("Whisper model canceled", self.canceled)

    @Slot(int, QThread)
    def _on_thread_finished(self, run_id: int, thread: QThread):
        if self._thread is not thread or self._run_id != run_id:
            return
        self._thread = None
        self._worker = None
        self._cancel_requested = False
        self._run_id = None
        self.thread_finished.emit(run_id)
