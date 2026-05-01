import logging
import time
from dataclasses import dataclass
from collections.abc import Callable

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import QWidget

from models.SubtitleGenerationDialogResult import SubtitleGenerationDialogResult
from services.subtitles.workers.SubtitleGenerationWorkers import SubtitleGenerationWorker
from services.subtitles.state.SubtitlePipelineState import SubtitlePipelinePhase, SubtitlePipelineRun
from services.subtitles.domain.SubtitleTiming import elapsed_ms_since, log_timing


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubtitleWorkerLaunchCallbacks:
    can_start_worker: Callable[[int, QThread, SubtitleGenerationWorker], bool]
    on_start_aborted: Callable[[int, QThread, SubtitleGenerationWorker], None]
    suspend_before_start: Callable[[], None]


@dataclass(frozen=True)
class SubtitleWorkerEventCallbacks:
    on_status_changed: Callable[[int, SubtitleGenerationWorker, str], None]
    on_progress_changed: Callable[[int, SubtitleGenerationWorker, int], None]
    on_details_changed: Callable[[int, SubtitleGenerationWorker, str], None]
    on_finished: Callable[[int, SubtitleGenerationWorker, str, bool, bool], None]
    on_failed: Callable[[int, SubtitleGenerationWorker, str, str], None]
    on_canceled: Callable[[int, SubtitleGenerationWorker], None]


class SubtitleGenerationJobRunner(QObject):
    thread_finished = Signal(int)

    def __init__(
        self,
        parent: QWidget,
        *,
        launch_callbacks: SubtitleWorkerLaunchCallbacks,
        event_callbacks: SubtitleWorkerEventCallbacks,
    ):
        super().__init__(parent)
        self._parent = parent
        self._launch_callbacks = launch_callbacks
        self._event_callbacks = event_callbacks

    def start(self, run: SubtitlePipelineRun, options: SubtitleGenerationDialogResult):
        launch_preparation_started_at = time.perf_counter()
        thread = QThread(self._parent)
        worker = SubtitleGenerationWorker(run.run_id, run.context.media_path, options)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.status_changed.connect(
            lambda text, run_id=run.run_id, worker=worker: self._event_callbacks.on_status_changed(run_id, worker, text),
            Qt.QueuedConnection,
        )
        worker.progress_changed.connect(
            lambda value, run_id=run.run_id, worker=worker: self._event_callbacks.on_progress_changed(run_id, worker, value),
            Qt.QueuedConnection,
        )
        worker.details_changed.connect(
            lambda text, run_id=run.run_id, worker=worker: self._event_callbacks.on_details_changed(run_id, worker, text),
            Qt.QueuedConnection,
        )
        worker.finished.connect(
            lambda output_path, auto_open, fallback, run_id=run.run_id, worker=worker: self._event_callbacks.on_finished(
                run_id,
                worker,
                output_path,
                auto_open,
                fallback,
            ),
            Qt.QueuedConnection,
        )
        worker.failed.connect(
            lambda error, diagnostics, run_id=run.run_id, worker=worker: self._event_callbacks.on_failed(
                run_id,
                worker,
                error,
                diagnostics,
            ),
            Qt.QueuedConnection,
        )
        worker.canceled.connect(
            lambda run_id=run.run_id, worker=worker: self._event_callbacks.on_canceled(run_id, worker),
            Qt.QueuedConnection,
        )

        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.canceled.connect(thread.quit)
        thread.finished.connect(lambda run_id=run.run_id: self.thread_finished.emit(run_id))
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        run.subtitle_thread = thread
        run.subtitle_worker = worker
        run.subtitle_cancel_requested = False
        QTimer.singleShot(
            0,
            lambda run_id=run.run_id, thread=thread, worker=worker: self._deferred_start_worker(
                run_id,
                thread,
                worker,
            ),
        )
        log_timing(
            logger,
            "Subtitle timing",
            "worker_launch_preparation",
            elapsed_ms_since(launch_preparation_started_at),
            run_id=run.run_id,
            media=run.context.media_path,
            output=options.output_path,
        )

    def _deferred_start_worker(
        self,
        run_id: int,
        thread: QThread,
        worker: SubtitleGenerationWorker,
    ):
        if not self._launch_callbacks.can_start_worker(run_id, thread, worker):
            self._abort_unstarted_worker(run_id, thread, worker)
            return

        if thread.isRunning():
            logger.debug("Skipping deferred subtitle worker thread start because thread is already running | run_id=%s", run_id)
            return

        self._launch_callbacks.suspend_before_start()
        thread.start()

    def _abort_unstarted_worker(
        self,
        run_id: int,
        thread: QThread,
        worker: SubtitleGenerationWorker,
    ):
        if thread.isRunning():
            return

        logger.debug("Cleaning up subtitle worker whose deferred start was canceled | run_id=%s", run_id)
        self._launch_callbacks.on_start_aborted(run_id, thread, worker)
        worker.deleteLater()
        thread.deleteLater()


def can_launch_subtitle_worker_run(
    run: SubtitlePipelineRun,
    thread: QThread,
    worker: SubtitleGenerationWorker,
) -> bool:
    return (
        run.subtitle_thread is thread
        and run.subtitle_worker is worker
        and run.phase in (
            SubtitlePipelinePhase.RUNNING,
            SubtitlePipelinePhase.CANCELING,
        )
    )
