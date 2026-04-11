from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import QWidget

from services.CudaRuntimeInstallWorker import CudaRuntimeInstallWorker
from services.SubtitleGenerationUiCoordinator import SubtitleGenerationUiCoordinator


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
        parent: QWidget,
        ui: SubtitleGenerationUiCoordinator,
    ):
        super().__init__(parent)
        self._ui = ui
        self._thread: QThread | None = None
        self._worker: CudaRuntimeInstallWorker | None = None
        self._cancel_requested = False
        self._run_id: int | None = None

    def start(self, run_id: int, missing_packages: list[str]) -> bool:
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
        self._ui.open_cuda_install_progress(
            missing_packages,
            on_cancel=self.cancel,
        )

        thread = QThread(self.parent())
        worker = CudaRuntimeInstallWorker(missing_packages)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.status_changed.connect(lambda text, run_id=run_id: self.status_changed.emit(run_id, text))
        worker.details_changed.connect(lambda text, run_id=run_id: self.details_changed.emit(run_id, text))
        worker.finished.connect(lambda run_id=run_id: self.finished.emit(run_id))
        worker.failed.connect(lambda error_text, run_id=run_id: self.failed.emit(run_id, error_text))
        worker.canceled.connect(lambda run_id=run_id: self.canceled.emit(run_id))

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
        thread.start()
        return True

    @Slot()
    def cancel(self):
        if self._worker is None:
            logger.debug("CUDA runtime flow cancel ignored because no worker is active")
            return

        if self._cancel_requested:
            logger.info("Repeated cancel request ignored for CUDA runtime flow")
            return

        self._cancel_requested = True
        logger.info("Cancel requested for CUDA runtime flow | run_id=%s", self._current_run_id())
        self._worker.cancel()
        self._ui.show_cuda_install_cancel_pending()

    def request_stop(self, *, force: bool):
        if self._worker is None:
            return

        if force:
            logger.warning("Force-stop requested for CUDA runtime flow | run_id=%s", self._current_run_id())
            self._worker.force_stop()
            return

        self.cancel()

    def is_active(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def _current_run_id(self) -> int | None:
        return self._run_id

    @Slot()
    def _on_thread_finished(self, run_id: int):
        logger.debug("CUDA runtime flow thread finished | run_id=%s", run_id)
        self._thread = None
        self._worker = None
        self._cancel_requested = False
        self._run_id = None
        self.thread_finished.emit(run_id)
