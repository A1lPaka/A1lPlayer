from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot

from services.runtime.CudaRuntimeInstallWorker import CudaRuntimeInstallWorker


logger = logging.getLogger(__name__)


class SubtitleCudaRuntimeFlow(QObject):
    status_changed = Signal(int, str)
    details_changed = Signal(int, str)
    finished = Signal(int)
    failed = Signal(int, str)
    canceled = Signal(int)
    thread_finished = Signal(int)

    def __init__(
        self,
        parent: QObject,
    ):
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: CudaRuntimeInstallWorker | None = None
        self._cancel_requested = False
        self._run_id: int | None = None

    def start(
        self,
        run_id: int,
        missing_packages: list[str],
    ) -> bool:
        if self.is_active():
            logger.warning(
                "Rejected CUDA runtime flow start because a previous install flow is still active | active_run_id=%s | new_run_id=%s",
                self._current_run_id(),
                run_id,
            )
            return False

        logger.debug(
            "Starting CUDA runtime flow helper | run_id=%s | packages=%s",
            run_id,
            ", ".join(missing_packages),
        )

        thread = QThread(self.parent())
        worker = CudaRuntimeInstallWorker(missing_packages)
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
        thread.finished.connect(lambda run_id=run_id: self._on_thread_finished(run_id))
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
            logger.debug("CUDA runtime flow stop ignored because no worker is active | force=%s", force)
            return False

        if force:
            logger.warning("Force-stop requested for CUDA runtime flow | run_id=%s", self._current_run_id())
            self._worker.force_stop()
            return True

        if self._cancel_requested:
            logger.info("Repeated stop request ignored for CUDA runtime flow")
            return False

        self._cancel_requested = True
        logger.info("Cancel requested for CUDA runtime flow | run_id=%s", self._current_run_id())
        self._worker.cancel()
        return True

    def is_active(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def _current_run_id(self) -> int | None:
        return self._run_id

    def _deferred_start(self, run_id: int, thread: QThread):
        if self._run_id != run_id:
            logger.debug("Skipping deferred CUDA runtime worker start for stale run | run_id=%s", run_id)
            return
        if self._thread is not thread or self._worker is None:
            logger.debug("Skipping deferred CUDA runtime worker start because worker references changed | run_id=%s", run_id)
            return
        if thread.isRunning():
            logger.debug("Skipping deferred CUDA runtime worker start because thread is already running | run_id=%s", run_id)
            return

        thread.start()

    def _is_active_worker_sender(self, event_name: str) -> bool:
        if self._worker is None:
            logger.debug("Ignoring %s because no CUDA runtime worker is active", event_name)
            return False

        sender = self.sender()
        if sender is not self._worker:
            logger.debug(
                "Ignoring %s from stale CUDA runtime worker | sender_matches_active=%s",
                event_name,
                sender is self._worker,
            )
            return False
        return True

    def _emit_active_worker_event(self, event_name: str, signal, *args):
        if not self._is_active_worker_sender(event_name):
            return
        run_id = self._current_run_id()
        if run_id is not None:
            signal.emit(run_id, *args)

    @Slot(str)
    def _on_worker_status_changed(self, text: str):
        self._emit_active_worker_event("CUDA runtime status update", self.status_changed, text)

    @Slot(str)
    def _on_worker_details_changed(self, text: str):
        self._emit_active_worker_event("CUDA runtime details update", self.details_changed, text)

    @Slot()
    def _on_worker_finished(self):
        self._emit_active_worker_event("CUDA runtime finished", self.finished)

    @Slot(str)
    def _on_worker_failed(self, error_text: str):
        self._emit_active_worker_event("CUDA runtime failed", self.failed, error_text)

    @Slot()
    def _on_worker_canceled(self):
        self._emit_active_worker_event("CUDA runtime canceled", self.canceled)

    @Slot()
    def _on_thread_finished(self, run_id: int):
        logger.debug("CUDA runtime flow thread finished | run_id=%s", run_id)
        self._thread = None
        self._worker = None
        self._cancel_requested = False
        self._run_id = None
        self.thread_finished.emit(run_id)
