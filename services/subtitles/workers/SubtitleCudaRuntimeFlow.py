from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot

from services.runtime.CudaRuntimeInstallWorker import CudaRuntimeInstallWorker
from services.subtitles.workers.WorkerEventGate import WorkerEventGate


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
        self._worker_events = WorkerEventGate()

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
        worker.status_changed.connect(
            lambda text, run_id=run_id, worker=worker: self._on_worker_status_changed(run_id, worker, text),
            Qt.QueuedConnection,
        )
        worker.details_changed.connect(
            lambda text, run_id=run_id, worker=worker: self._on_worker_details_changed(run_id, worker, text),
            Qt.QueuedConnection,
        )
        worker.finished.connect(
            lambda run_id=run_id, worker=worker: self._on_worker_finished(run_id, worker),
            Qt.QueuedConnection,
        )
        worker.failed.connect(
            lambda error_text, run_id=run_id, worker=worker: self._on_worker_failed(run_id, worker, error_text),
            Qt.QueuedConnection,
        )
        worker.canceled.connect(
            lambda run_id=run_id, worker=worker: self._on_worker_canceled(run_id, worker),
            Qt.QueuedConnection,
        )

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
        self._worker_events.start(run_id, worker)
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

    def _emit_active_worker_event(
        self,
        event_name: str,
        run_id: int,
        worker: CudaRuntimeInstallWorker,
        signal,
        *args,
        terminal: bool = False,
    ):
        if self._worker_events.emit_if_current(run_id, worker, signal, *args, terminal=terminal):
            return
        logger.debug(
            "Ignoring %s from stale CUDA runtime worker | run_id=%s | active_run_id=%s | worker_matches_active=%s",
            event_name,
            run_id,
            self._run_id,
            worker is self._worker,
        )

    def _on_worker_status_changed(self, run_id: int, worker: CudaRuntimeInstallWorker, text: str):
        self._emit_active_worker_event("CUDA runtime status update", run_id, worker, self.status_changed, text)

    def _on_worker_details_changed(self, run_id: int, worker: CudaRuntimeInstallWorker, text: str):
        self._emit_active_worker_event("CUDA runtime details update", run_id, worker, self.details_changed, text)

    def _on_worker_finished(self, run_id: int, worker: CudaRuntimeInstallWorker):
        self._emit_active_worker_event("CUDA runtime finished", run_id, worker, self.finished, terminal=True)

    def _on_worker_failed(self, run_id: int, worker: CudaRuntimeInstallWorker, error_text: str):
        self._emit_active_worker_event("CUDA runtime failed", run_id, worker, self.failed, error_text, terminal=True)

    def _on_worker_canceled(self, run_id: int, worker: CudaRuntimeInstallWorker):
        self._emit_active_worker_event("CUDA runtime canceled", run_id, worker, self.canceled, terminal=True)

    @Slot(int, QThread)
    def _on_thread_finished(self, run_id: int, thread: QThread):
        logger.debug("CUDA runtime flow thread finished | run_id=%s", run_id)
        if self._thread is not thread or self._run_id != run_id:
            logger.debug("Ignoring stale CUDA runtime flow thread finish | run_id=%s", run_id)
            return

        self._worker_events.finish_thread(run_id, self._worker)
        self._thread = None
        self._worker = None
        self._cancel_requested = False
        self._run_id = None
        self.thread_finished.emit(run_id)
