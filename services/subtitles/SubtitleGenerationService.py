import logging
import time
from dataclasses import dataclass, field, replace
from enum import Enum, auto
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import QWidget

from services.MediaSettingsStore import MediaSettingsStore
from services.MediaLibraryService import SubtitleAttachResult
from services.subtitles.SubtitleCudaRuntimeFlow import SubtitleCudaRuntimeFlow
from services.subtitles.SubtitleGenerationPreflight import AudioStreamProbeState, SubtitleGenerationPreflight
from services.subtitles.SubtitleGenerationUiCoordinator import SubtitleGenerationUiCoordinator
from services.subtitles.SubtitleGenerationWorkers import AudioStreamProbeWorker, SubtitleGenerationWorker
from services.subtitles.SubtitleMaker import (
    get_missing_windows_cuda_runtime_packages,
)
from services.subtitles.SubtitleTiming import elapsed_ms_since, log_timing
from ui.MessageBoxService import (
    prompt_cuda_runtime_choice,
    show_cuda_runtime_install_canceled,
    show_cuda_runtime_install_failed,
    show_audio_stream_inspection_warning,
    show_subtitle_auto_load_failed,
    show_subtitle_created,
    show_subtitle_created_with_fallback_name,
    show_subtitle_created_not_loaded_due_to_context_change,
    show_subtitle_generation_already_running,
    show_subtitle_generation_canceled,
    show_subtitle_generation_failed,
)
from ui.PlayerWindow import PlayerWindow
from ui.SubtitleGenerationDialog import SubtitleGenerationDialogResult


logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from services.MediaLibraryService import MediaLibraryService


@dataclass(frozen=True)
class SubtitleGenerationContext:
    media_path: str
    request_id: int | None


class SubtitleServiceState(Enum):
    IDLE = auto()
    DIALOG_OPEN = auto()
    SHUTTING_DOWN = auto()


class SubtitlePipelinePhase(Enum):
    STARTING = auto()
    RUNNING = auto()
    CANCELING = auto()
    SUCCEEDED = auto()
    FAILED = auto()
    CANCELED = auto()


class SubtitlePipelineResult(Enum):
    SUCCEEDED = auto()
    FAILED = auto()
    CANCELED = auto()


class SubtitleAutoOpenOutcome(Enum):
    LOADED = auto()
    CONTEXT_CHANGED = auto()
    LOAD_FAILED = auto()


class SubtitlePipelineTask(Enum):
    NONE = auto()
    CUDA_INSTALL = auto()
    SUBTITLE_GENERATION = auto()


@dataclass
class SubtitlePipelineRun:
    run_id: int
    context: SubtitleGenerationContext
    requested_options: SubtitleGenerationDialogResult
    subtitle_options: SubtitleGenerationDialogResult | None = None
    phase: SubtitlePipelinePhase = SubtitlePipelinePhase.STARTING
    task: SubtitlePipelineTask = SubtitlePipelineTask.NONE
    subtitle_thread: QThread | None = None
    subtitle_worker: SubtitleGenerationWorker | None = None
    subtitle_cancel_requested: bool = False
    started_at: float = field(default_factory=time.perf_counter)

    def blocks_new_requests(self) -> bool:
        return self.phase in (
            SubtitlePipelinePhase.STARTING,
            SubtitlePipelinePhase.RUNNING,
            SubtitlePipelinePhase.CANCELING,
        )

    def accepts_stop_requests(self) -> bool:
        return self.phase in (
            SubtitlePipelinePhase.RUNNING,
            SubtitlePipelinePhase.CANCELING,
        )

    def keeps_shutdown_pending(self) -> bool:
        return self.task != SubtitlePipelineTask.NONE and self.phase in (
            SubtitlePipelinePhase.RUNNING,
            SubtitlePipelinePhase.CANCELING,
        )


class SubtitleGenerationService(QObject):
    shutdown_finished = Signal()
    _PLAYBACK_INTERRUPTION_OWNER = "subtitle_generation"

    def __init__(
        self,
        parent: QWidget,
        player_window: PlayerWindow,
        store: MediaSettingsStore,
        media_library: "MediaLibraryService",
    ):
        super().__init__(parent)
        self._parent = parent
        self._player = player_window
        self._store = store
        self._media_library = media_library
        self._ui = SubtitleGenerationUiCoordinator(
            parent,
            theme_color_getter=lambda: self._player.theme_color,
        )
        self._preflight = SubtitleGenerationPreflight(parent)
        self._service_state = SubtitleServiceState.IDLE
        self._last_result: SubtitlePipelineResult | None = None
        self._active_run: SubtitlePipelineRun | None = None
        self._pending_subtitle_thread_run_ids: set[int] = set()
        self._next_run_id = 1
        self._shutdown_completed = False
        self._force_shutdown_requested = False
        self._playback_takeover = self._player.playback.create_interruption_lease(
            self._PLAYBACK_INTERRUPTION_OWNER,
        )
        self._player_ui_suspend_lease = None
        self._audio_stream_probe_media_path: str | None = None
        self._audio_stream_probe_state = AudioStreamProbeState.IDLE
        self._cached_audio_streams = None
        self._cached_audio_stream_error: str | None = None
        self._audio_stream_probe_request_id = 0
        self._current_audio_stream_probe_request_id: int | None = None
        self._audio_stream_probe_workers: dict[int, AudioStreamProbeWorker] = {}
        self._dialog_request_started_at: float | None = None
        self._dialog_request_media_path: str | None = None
        self._cuda_runtime_flow = SubtitleCudaRuntimeFlow(parent)
        self._cuda_runtime_flow.status_changed.connect(self._on_worker_status_changed)
        self._cuda_runtime_flow.details_changed.connect(self._on_worker_details_changed)
        self._cuda_runtime_flow.finished.connect(self._on_cuda_runtime_install_finished)
        self._cuda_runtime_flow.failed.connect(self._on_cuda_runtime_install_failed)
        self._cuda_runtime_flow.canceled.connect(self._on_cuda_runtime_install_canceled)
        self._cuda_runtime_flow.thread_finished.connect(self._on_cuda_runtime_flow_thread_finished)

    def generate_subtitle(self) -> bool:
        current_media_path = self._player.playback.current_media_path()
        if not current_media_path:
            logger.info("Subtitle generation requested without an active media item")
            return False

        if self._shutdown_completed or self._service_state == SubtitleServiceState.SHUTTING_DOWN:
            logger.info("Subtitle generation request ignored because shutdown is in progress")
            return False

        if self._active_run is not None and self._active_run.blocks_new_requests():
            self._ui.focus_active_dialog()
            show_subtitle_generation_already_running(self._parent)
            logger.info("Subtitle generation request ignored because another background task is running")
            return False

        if self._service_state == SubtitleServiceState.DIALOG_OPEN:
            self._ui.focus_active_dialog()
            logger.info("Subtitle generation request focused the existing generation dialog")
            return False

        if not self._transition_service_state(
            SubtitleServiceState.DIALOG_OPEN,
            "open generation dialog",
            allowed=(SubtitleServiceState.IDLE,),
        ):
            return False

        self._playback_takeover.acquire()
        self._dialog_request_started_at = time.perf_counter()
        self._dialog_request_media_path = current_media_path
        self._ui.open_generation_dialog(
            current_media_path,
            on_generate=self._start_subtitle_generation,
            on_cancel=self._on_generation_dialog_canceled,
        )
        self._load_generation_audio_tracks_async(current_media_path)
        return True

    def _start_subtitle_generation(self, options: SubtitleGenerationDialogResult):
        self._log_dialog_confirm_timing(options.output_path)
        if self._service_state != SubtitleServiceState.DIALOG_OPEN:
            logger.warning(
                "Rejected subtitle generation start because the generation dialog is not active | state=%s",
                self._service_state.name,
            )
            return

        current_media_path = self._player.playback.current_media_path()
        if not current_media_path:
            logger.warning("Subtitle generation aborted because current media path disappeared before launch")
            self._transition_back_to_dialog("media path disappeared before launch")
            return

        generation_context = self._capture_current_generation_context()
        if generation_context is None:
            logger.warning("Subtitle generation aborted because playback context is unavailable before launch")
            self._transition_back_to_dialog("playback context unavailable before launch")
            return

        run = self._begin_pipeline_run(generation_context, options)
        preflight_started_at = time.perf_counter()
        validation_result = self._preflight.validate_generation_request(
            current_media_path,
            options,
            probe_state=self._get_audio_stream_probe_state(current_media_path),
            audio_streams=self._get_cached_audio_streams_for_media(current_media_path),
            probe_error=self._get_cached_audio_stream_error_for_media(current_media_path),
        )
        log_timing(
            logger,
            "Subtitle timing",
            "preflight_validation",
            elapsed_ms_since(preflight_started_at),
            run_id=run.run_id,
            media=run.context.media_path,
            output=options.output_path,
        )
        if not validation_result.is_valid:
            self._discard_starting_run("subtitle generation preflight failed")
            return

        resolved_options = self._resolve_cuda_runtime_options(options, run)
        if resolved_options is None:
            if self._active_run is run and run.phase == SubtitlePipelinePhase.STARTING:
                self._discard_starting_run("subtitle launch postponed or canceled during CUDA resolution")
            return

        generation_context = self._capture_current_generation_context()
        if generation_context is None:
            logger.warning("Subtitle generation aborted because playback context is unavailable before launch")
            self._active_run = None
            self._transition_back_to_dialog("playback context unavailable before launch")
            return

        run.context = generation_context
        run.subtitle_options = resolved_options
        self._launch_subtitle_generation(run, resolved_options)

    def _launch_subtitle_generation(
        self,
        run: SubtitlePipelineRun,
        options: SubtitleGenerationDialogResult,
    ):
        if run is not self._active_run:
            logger.debug("Ignoring subtitle worker launch for stale run | run_id=%s", run.run_id)
            return
        if run.phase not in (SubtitlePipelinePhase.STARTING, SubtitlePipelinePhase.RUNNING):
            logger.warning(
                "Rejected subtitle worker launch because run phase is not launchable | run_id=%s | phase=%s",
                run.run_id,
                run.phase.name,
            )
            return

        self._set_run_phase(run, SubtitlePipelinePhase.RUNNING, "launch subtitle generation worker")
        if self._service_state != SubtitleServiceState.SHUTTING_DOWN:
            self._transition_service_state(
                SubtitleServiceState.IDLE,
                "generation dialog replaced by subtitle progress",
                allowed=(SubtitleServiceState.DIALOG_OPEN, SubtitleServiceState.IDLE),
            )

        logger.info(
            "Launching subtitle generation | run_id=%s | media=%s | request_id=%s | output=%s | format=%s | requested_device=%s | model=%s",
            run.run_id,
            run.context.media_path,
            run.context.request_id,
            options.output_path,
            options.output_format,
            options.device or "auto",
            options.model_size,
        )
        run.task = SubtitlePipelineTask.SUBTITLE_GENERATION
        self._ui.open_generation_progress(options, on_cancel=self._request_active_task_stop)

        launch_preparation_started_at = time.perf_counter()
        thread = QThread(self._parent)
        worker = SubtitleGenerationWorker(run.run_id, run.context.media_path, options)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.status_changed.connect(self._on_worker_status_changed_from_worker, Qt.QueuedConnection)
        worker.progress_changed.connect(self._on_worker_progress_changed_from_worker, Qt.QueuedConnection)
        worker.details_changed.connect(self._on_worker_details_changed_from_worker, Qt.QueuedConnection)
        worker.finished.connect(self._on_subtitle_generation_finished_from_worker, Qt.QueuedConnection)
        worker.failed.connect(self._on_subtitle_generation_failed_from_worker, Qt.QueuedConnection)
        worker.canceled.connect(self._on_subtitle_generation_canceled_from_worker, Qt.QueuedConnection)

        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.canceled.connect(thread.quit)
        thread.finished.connect(
            lambda run_id=run.run_id: self._on_background_task_thread_finished(
                run_id,
                SubtitlePipelineTask.SUBTITLE_GENERATION,
            )
        )
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        run.subtitle_thread = thread
        run.subtitle_worker = worker
        run.subtitle_cancel_requested = False
        self._pending_subtitle_thread_run_ids.add(run.run_id)
        QTimer.singleShot(
            0,
            lambda run_id=run.run_id, thread=thread: self._deferred_suspend_and_start_subtitle_worker(run_id, thread),
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

    def _resolve_cuda_runtime_options(
        self,
        options: SubtitleGenerationDialogResult,
        run: SubtitlePipelineRun,
    ) -> SubtitleGenerationDialogResult | None:
        if options.device != "cuda":
            return options

        missing_packages = get_missing_windows_cuda_runtime_packages()
        if not missing_packages:
            return options

        logger.info(
            "CUDA runtime missing for subtitle generation | run_id=%s | media=%s | request_id=%s | packages=%s",
            run.run_id,
            run.context.media_path,
            run.context.request_id,
            ", ".join(missing_packages),
        )
        choice = prompt_cuda_runtime_choice(self._parent, missing_packages)
        if choice == "cancel":
            logger.info("User canceled subtitle generation after CUDA runtime prompt | run_id=%s", run.run_id)
            return None

        if choice == "cpu":
            logger.info("User switched subtitle generation from CUDA to CPU | run_id=%s", run.run_id)
            return replace(options, device="cpu")

        run.subtitle_options = options
        self._start_cuda_runtime_install(run, missing_packages)
        return None

    def _start_cuda_runtime_install(
        self,
        run: SubtitlePipelineRun,
        missing_packages: list[str],
    ):
        if run is not self._active_run:
            logger.debug("Ignoring CUDA runtime install start for stale run | run_id=%s", run.run_id)
            return
        if run.phase != SubtitlePipelinePhase.STARTING:
            logger.warning(
                "Rejected CUDA runtime install start because run phase is not launchable | run_id=%s | phase=%s",
                run.run_id,
                run.phase.name,
            )
            return

        self._set_run_phase(run, SubtitlePipelinePhase.RUNNING, "start CUDA runtime install")
        if self._service_state != SubtitleServiceState.SHUTTING_DOWN:
            self._transition_service_state(
                SubtitleServiceState.IDLE,
                "generation dialog replaced by CUDA runtime progress",
                allowed=(SubtitleServiceState.DIALOG_OPEN, SubtitleServiceState.IDLE),
            )

        logger.info(
            "Starting CUDA runtime install flow | run_id=%s | media=%s | request_id=%s | packages=%s",
            run.run_id,
            run.context.media_path,
            run.context.request_id,
            ", ".join(missing_packages),
        )
        run.task = SubtitlePipelineTask.CUDA_INSTALL
        self._ui.open_cuda_install_progress(
            missing_packages,
            on_cancel=self._request_active_task_stop,
        )
        if not self._cuda_runtime_flow.start(run.run_id, missing_packages):
            logger.error("CUDA runtime install flow could not be started | run_id=%s", run.run_id)
            self._complete_run(
                run.run_id,
                SubtitlePipelinePhase.FAILED,
                close_progress=True,
            )
            if self._service_state != SubtitleServiceState.SHUTTING_DOWN:
                show_cuda_runtime_install_failed(self._parent, "GPU runtime installation could not be started.")
            return

    def _deferred_suspend_and_start_subtitle_worker(self, run_id: int, thread: QThread):
        run = self._require_active_run(run_id, "deferred subtitle worker launch")
        if run is None:
            return

        if run.subtitle_thread is not thread or run.subtitle_worker is None:
            logger.debug("Skipping deferred subtitle worker launch because worker references changed | run_id=%s", run_id)
            return

        if run.phase not in (
            SubtitlePipelinePhase.RUNNING,
            SubtitlePipelinePhase.CANCELING,
        ):
            logger.debug(
                "Skipping deferred subtitle worker launch because run phase changed | run_id=%s | phase=%s",
                run_id,
                run.phase.name,
            )
            return

        self._suspend_player_ui_for_generation()
        QTimer.singleShot(
            0,
            lambda run_id=run_id, thread=thread: self._deferred_start_subtitle_worker(run_id, thread),
        )

    def _deferred_start_subtitle_worker(self, run_id: int, thread: QThread):
        run = self._require_active_run(run_id, "deferred subtitle worker thread start")
        if run is None:
            return

        if run.subtitle_thread is not thread or run.subtitle_worker is None:
            logger.debug("Skipping deferred subtitle worker thread start because worker references changed | run_id=%s", run_id)
            return

        if thread.isRunning():
            logger.debug("Skipping deferred subtitle worker thread start because thread is already running | run_id=%s", run_id)
            return

        thread.start()

    @Slot()
    def _request_active_task_stop(self, force: bool = False):
        run = self._active_run
        if run is None or not run.accepts_stop_requests():
            logger.debug(
                "Active task stop ignored because pipeline is not stoppable | run_phase=%s | force=%s",
                run.phase.name if run is not None else "<none>",
                force,
            )
            return

        if run.task == SubtitlePipelineTask.NONE:
            logger.debug("Active task stop ignored because no pipeline task is active | force=%s", force)
            return

        self._set_run_phase(run, SubtitlePipelinePhase.CANCELING, f"request stop for {run.task.name.lower()}")

        run_id = self._current_run_id()
        if run.task == SubtitlePipelineTask.SUBTITLE_GENERATION:
            if run.subtitle_worker is None:
                logger.debug("Subtitle generation stop ignored because no worker is active | force=%s", force)
                return

            if force:
                logger.warning("Force-stop requested for subtitle generation worker | run_id=%s", run_id)
                run.subtitle_worker.force_stop()
                return

            if run.subtitle_cancel_requested:
                logger.info("Repeated stop request ignored for subtitle generation worker")
                return

            run.subtitle_cancel_requested = True
            logger.info("Cancel requested for subtitle generation worker | run_id=%s", run_id)
            run.subtitle_worker.cancel()
            self._ui.show_subtitle_cancel_pending()
            return

        requested = self._cuda_runtime_flow.request_stop(force=force)
        if not requested:
            return

        if force:
            logger.warning("Force-stop requested for CUDA runtime install flow | run_id=%s", run_id)
            return

        logger.info("Cancel requested for CUDA runtime install flow | run_id=%s", run_id)
        self._ui.show_cuda_install_cancel_pending()

    def has_active_tasks(self) -> bool:
        if self._pending_subtitle_thread_run_ids or self._cuda_runtime_flow.is_active():
            return True

        run = self._active_run
        if run is None:
            return False

        return run.keeps_shutdown_pending()

    def is_shutdown_in_progress(self) -> bool:
        return self._service_state == SubtitleServiceState.SHUTTING_DOWN and not self._shutdown_completed

    def begin_shutdown(self) -> bool:
        if self._shutdown_completed:
            logger.debug("Subtitle generation service shutdown requested after completion")
            return False

        if self._service_state == SubtitleServiceState.SHUTTING_DOWN:
            logger.info(
                "Subtitle generation service shutdown already in progress | force_requested=%s",
                self._force_shutdown_requested,
            )
            return self.has_active_tasks()

        logger.info("Subtitle generation service async shutdown started | state=%s", self._service_state.name)
        self._transition_service_state(
            SubtitleServiceState.SHUTTING_DOWN,
            "begin graceful shutdown",
            allowed=tuple(SubtitleServiceState),
        )
        self._force_shutdown_requested = False
        self._ui.close_generation_dialog()
        self._request_active_task_stop(force=False)
        self._invalidate_active_audio_stream_probe_request("shutdown")
        self._complete_shutdown_if_possible()
        return self.has_active_tasks()

    def begin_force_shutdown(self) -> bool:
        if self._shutdown_completed:
            logger.debug("Subtitle generation service force shutdown requested after completion")
            return False

        if self._service_state != SubtitleServiceState.SHUTTING_DOWN:
            logger.warning("Subtitle generation service force shutdown requested before graceful shutdown")
            self._transition_service_state(
                SubtitleServiceState.SHUTTING_DOWN,
                "begin force shutdown",
                allowed=tuple(SubtitleServiceState),
            )
            self._ui.close_generation_dialog()

        if self._force_shutdown_requested:
            logger.info("Repeated force shutdown request ignored for subtitle generation service")
            return self.has_active_tasks()

        logger.warning("Subtitle generation service async force shutdown started")
        self._force_shutdown_requested = True
        self._ui.close_progress_dialog()
        self._request_active_task_stop(force=True)
        self._invalidate_active_audio_stream_probe_request("shutdown")
        self._complete_shutdown_if_possible()
        return self.has_active_tasks()

    def _finalize_shutdown_service_state(self):
        self._ui.close_progress_dialog()
        self._active_run = None
        self._invalidate_active_audio_stream_probe_request("finalize-shutdown")
        self._release_playback_takeover(resume_playback=False)
        self._shutdown_completed = True
        self._force_shutdown_requested = False

    def _complete_shutdown_if_possible(self):
        if self._service_state != SubtitleServiceState.SHUTTING_DOWN or self._shutdown_completed:
            return

        if self.has_active_tasks():
            active_task = self._active_run.task.name if self._active_run is not None else "NONE"
            active_phase = self._active_run.phase.name if self._active_run is not None else "<none>"
            logger.debug(
                "Subtitle generation service shutdown still waiting for background tasks | task=%s | phase=%s | force_requested=%s",
                active_task,
                active_phase,
                self._force_shutdown_requested,
            )
            return

        self._finalize_shutdown_service_state()
        logger.info("Subtitle generation service shutdown finished")
        self.shutdown_finished.emit()

    @Slot()
    def _on_generation_dialog_canceled(self):
        if self._service_state != SubtitleServiceState.DIALOG_OPEN:
            return

        self._invalidate_active_audio_stream_probe_request("dialog closed")
        self._clear_dialog_request_timing()
        logger.info("Subtitle generation dialog closed without launching a job")
        self._transition_service_state(
            SubtitleServiceState.IDLE,
            "close generation dialog",
            allowed=(SubtitleServiceState.DIALOG_OPEN,),
        )
        self._release_playback_takeover(resume_playback=True)

    def _on_background_task_thread_finished(self, run_id: int, task: SubtitlePipelineTask):
        if task == SubtitlePipelineTask.SUBTITLE_GENERATION:
            self._pending_subtitle_thread_run_ids.discard(run_id)

        run = self._active_run if self._active_run is not None and self._active_run.run_id == run_id else None
        if run is not None and task == SubtitlePipelineTask.SUBTITLE_GENERATION:
            self._clear_subtitle_runtime(run)

        if run is not None:
            logger.debug(
                "Background task thread finished for active subtitle pipeline run | run_id=%s | task=%s | phase=%s",
                run_id,
                task.name,
                run.phase.name,
            )
        else:
            logger.debug(
                "Background task thread finished for stale subtitle pipeline run | run_id=%s | task=%s | state=%s",
                run_id,
                task.name,
                self._service_state.name,
            )

        if run is not None and self._run_is_terminal(run):
            self._active_run = None
        self._complete_shutdown_if_possible()

    def _on_cuda_runtime_flow_thread_finished(self, run_id: int):
        self._on_background_task_thread_finished(run_id, SubtitlePipelineTask.CUDA_INSTALL)

    def _current_run_id_for_active_subtitle_worker(self, event_name: str) -> int | None:
        run = self._active_run
        if run is None or run.subtitle_worker is None:
            logger.debug("Ignoring %s because no subtitle worker is active", event_name)
            return None

        sender = self.sender()
        if sender is not run.subtitle_worker:
            logger.debug(
                "Ignoring %s from stale subtitle worker | sender_matches_active=%s",
                event_name,
                sender is run.subtitle_worker,
            )
            return None

        return run.run_id

    @Slot(str)
    def _on_worker_status_changed_from_worker(self, text: str):
        run_id = self._current_run_id_for_active_subtitle_worker("status update")
        if run_id is None:
            return
        self._on_worker_status_changed(run_id, text)

    @Slot(int)
    def _on_worker_progress_changed_from_worker(self, value: int):
        run_id = self._current_run_id_for_active_subtitle_worker("progress update")
        if run_id is None:
            return
        self._on_worker_progress_changed(run_id, value)

    @Slot(str)
    def _on_worker_details_changed_from_worker(self, text: str):
        run_id = self._current_run_id_for_active_subtitle_worker("details update")
        if run_id is None:
            return
        self._on_worker_details_changed(run_id, text)

    @Slot(str, bool, bool)
    def _on_subtitle_generation_finished_from_worker(self, output_path: str, auto_open: bool, used_fallback_output_path: bool):
        run_id = self._current_run_id_for_active_subtitle_worker("subtitle generation finished")
        if run_id is None:
            return
        self._on_subtitle_generation_finished(run_id, output_path, auto_open, used_fallback_output_path)

    @Slot(str, str)
    def _on_subtitle_generation_failed_from_worker(self, error_text: str, diagnostics: str):
        run_id = self._current_run_id_for_active_subtitle_worker("subtitle generation failed")
        if run_id is None:
            return
        self._on_subtitle_generation_failed(run_id, error_text, diagnostics)

    @Slot()
    def _on_subtitle_generation_canceled_from_worker(self):
        run_id = self._current_run_id_for_active_subtitle_worker("subtitle generation canceled")
        if run_id is None:
            return
        self._on_subtitle_generation_canceled(run_id)

    def _on_worker_status_changed(self, run_id: int, text: str):
        if not self._is_current_run_event(run_id, "status update"):
            return
        self._ui.update_progress_status(text)

    def _on_worker_progress_changed(self, run_id: int, value: int):
        if not self._is_current_run_event(run_id, "progress update"):
            return
        self._ui.update_progress(value)

    def _on_worker_details_changed(self, run_id: int, text: str):
        if not self._is_current_run_event(run_id, "details update"):
            return
        self._ui.update_progress_details(text)

    def _on_subtitle_generation_finished(self, run_id: int, output_path: str, auto_open: bool, used_fallback_output_path: bool):
        run = self._require_active_run(run_id, "subtitle generation finished")
        if run is None:
            return

        logger.info(
            "Subtitle generation finished | run_id=%s | media=%s | request_id=%s | output=%s | auto_open=%s",
            run.run_id,
            run.context.media_path,
            run.context.request_id,
            output_path,
            auto_open,
        )

        self._complete_run(
            run_id,
            SubtitlePipelinePhase.SUCCEEDED,
            close_progress=True,
        )

        if self._service_state == SubtitleServiceState.SHUTTING_DOWN:
            return

        auto_open_outcome = SubtitleAutoOpenOutcome.LOADED
        if auto_open:
            attach_result = self._media_library.attach_subtitle(
                output_path,
                source="generated",
                save_last_dir=True,
                guard_media_path=run.context.media_path,
                guard_request_id=run.context.request_id,
            )
            if attach_result == SubtitleAttachResult.CONTEXT_CHANGED:
                auto_open_outcome = SubtitleAutoOpenOutcome.CONTEXT_CHANGED
            elif attach_result == SubtitleAttachResult.LOAD_FAILED:
                logger.error("Generated subtitle could not be auto-loaded into playback | run_id=%s | output=%s", run.run_id, output_path)
                auto_open_outcome = SubtitleAutoOpenOutcome.LOAD_FAILED
        else:
            self._store.save_last_open_dir(output_path)

        self._show_generation_success_outcome(
            output_path,
            auto_open_outcome,
            used_fallback_output_path=used_fallback_output_path,
            requested_output_path=(run.subtitle_options or run.requested_options).output_path,
        )

    def _on_subtitle_generation_failed(self, run_id: int, error_text: str, diagnostics: str):
        run = self._require_active_run(run_id, "subtitle generation failed")
        if run is None:
            return

        if diagnostics:
            logger.error(
                "Subtitle generation failed | run_id=%s | message=%s | diagnostics=%s",
                run.run_id,
                error_text,
                diagnostics,
            )
        else:
            logger.error("Subtitle generation failed | run_id=%s | message=%s", run.run_id, error_text)

        self._complete_run(
            run_id,
            SubtitlePipelinePhase.FAILED,
            close_progress=True,
        )

        if self._service_state == SubtitleServiceState.SHUTTING_DOWN:
            return

        show_subtitle_generation_failed(self._parent, error_text)

    def _on_subtitle_generation_canceled(self, run_id: int):
        run = self._require_active_run(run_id, "subtitle generation canceled")
        if run is None:
            return

        logger.info("Subtitle generation canceled | run_id=%s", run.run_id)
        self._complete_run(
            run_id,
            SubtitlePipelinePhase.CANCELED,
            close_progress=True,
        )

        if self._service_state == SubtitleServiceState.SHUTTING_DOWN:
            return

        show_subtitle_generation_canceled(self._parent)

    def _on_cuda_runtime_install_finished(self, run_id: int):
        run = self._require_active_run(run_id, "CUDA runtime install finished")
        if run is None:
            return

        logger.info("CUDA runtime install flow finished | run_id=%s", run.run_id)
        self._ui.close_progress_dialog()

        if run.phase == SubtitlePipelinePhase.CANCELING:
            logger.info("Ignoring CUDA runtime completion because pipeline cancellation is already in progress | run_id=%s", run.run_id)
            self._complete_run(
                run_id,
                SubtitlePipelinePhase.CANCELED,
                close_progress=False,
            )
            return

        if self._service_state == SubtitleServiceState.SHUTTING_DOWN:
            self._complete_run(
                run_id,
                SubtitlePipelinePhase.CANCELED,
                close_progress=False,
            )
            return

        if run.subtitle_options is None:
            logger.warning("CUDA runtime install finished without pending subtitle generation options | run_id=%s", run.run_id)
            self._complete_run(
                run_id,
                SubtitlePipelinePhase.FAILED,
                close_progress=False,
            )
            show_cuda_runtime_install_failed(
                self._parent,
                "GPU runtime installation finished without subtitle options.",
            )
            return

        self._launch_subtitle_generation(run, run.subtitle_options)

    def _on_cuda_runtime_install_failed(self, run_id: int, error_text: str):
        run = self._require_active_run(run_id, "CUDA runtime install failed")
        if run is None:
            return

        logger.error("CUDA runtime install failed | run_id=%s | message=%s", run.run_id, error_text)
        self._complete_run(
            run_id,
            SubtitlePipelinePhase.FAILED,
            close_progress=True,
        )

        if self._service_state == SubtitleServiceState.SHUTTING_DOWN:
            return

        show_cuda_runtime_install_failed(self._parent, error_text)

    def _on_cuda_runtime_install_canceled(self, run_id: int):
        run = self._require_active_run(run_id, "CUDA runtime install canceled")
        if run is None:
            return

        logger.info("CUDA runtime install canceled | run_id=%s", run.run_id)
        self._complete_run(
            run_id,
            SubtitlePipelinePhase.CANCELED,
            close_progress=True,
        )

        if self._service_state == SubtitleServiceState.SHUTTING_DOWN:
            return

        show_cuda_runtime_install_canceled(self._parent)

    def _show_generation_success_outcome(
        self,
        output_path: str,
        auto_open_outcome: SubtitleAutoOpenOutcome,
        *,
        used_fallback_output_path: bool,
        requested_output_path: str | None,
    ):
        if auto_open_outcome == SubtitleAutoOpenOutcome.CONTEXT_CHANGED:
            show_subtitle_created_not_loaded_due_to_context_change(self._parent, output_path)
            return

        if auto_open_outcome == SubtitleAutoOpenOutcome.LOAD_FAILED:
            show_subtitle_auto_load_failed(self._parent, output_path)
            return

        if used_fallback_output_path:
            show_subtitle_created_with_fallback_name(
                self._parent,
                requested_output_path or output_path,
                output_path,
            )
            return

        show_subtitle_created(self._parent, output_path)

    def _capture_current_generation_context(self) -> SubtitleGenerationContext | None:
        media_path = self._player.playback.current_media_path()
        if not media_path:
            return None

        return SubtitleGenerationContext(
            media_path=media_path,
            request_id=self._player.playback.current_request_id(),
        )

    def _begin_pipeline_run(
        self,
        generation_context: SubtitleGenerationContext,
        options: SubtitleGenerationDialogResult,
    ) -> SubtitlePipelineRun:
        run = SubtitlePipelineRun(
            run_id=self._next_run_id,
            context=generation_context,
            requested_options=options,
        )
        self._next_run_id += 1
        self._active_run = run
        return run

    def _discard_starting_run(self, reason: str):
        run = self._active_run
        if run is not None:
            logger.debug("Discarding starting subtitle pipeline run | run_id=%s | reason=%s", run.run_id, reason)
            self._clear_subtitle_runtime(run)
        self._active_run = None
        self._transition_back_to_dialog(reason)

    def _set_run_phase(self, run: SubtitlePipelineRun, phase: SubtitlePipelinePhase, reason: str):
        if run.phase == phase:
            return
        logger.debug(
            "Subtitle pipeline run phase transition | run_id=%s | from=%s | to=%s | reason=%s",
            run.run_id,
            run.phase.name,
            phase.name,
            reason,
        )
        run.phase = phase

    def _clear_subtitle_runtime(self, run: SubtitlePipelineRun):
        run.subtitle_thread = None
        run.subtitle_worker = None
        run.subtitle_cancel_requested = False

    def _run_is_terminal(self, run: SubtitlePipelineRun) -> bool:
        return run.phase in (
            SubtitlePipelinePhase.SUCCEEDED,
            SubtitlePipelinePhase.FAILED,
            SubtitlePipelinePhase.CANCELED,
        )

    def _run_is_waiting_for_thread_cleanup(self, run: SubtitlePipelineRun) -> bool:
        if run.task == SubtitlePipelineTask.SUBTITLE_GENERATION:
            return run.run_id in self._pending_subtitle_thread_run_ids
        if run.task == SubtitlePipelineTask.CUDA_INSTALL:
            return self._cuda_runtime_flow.is_active()
        return False

    def _transition_back_to_dialog(self, reason: str):
        self._transition_service_state(
            SubtitleServiceState.DIALOG_OPEN,
            reason,
            allowed=(SubtitleServiceState.DIALOG_OPEN,),
        )

    def _complete_run(
        self,
        run_id: int,
        terminal_phase: SubtitlePipelinePhase,
        *,
        close_progress: bool,
    ):
        if terminal_phase not in (
            SubtitlePipelinePhase.SUCCEEDED,
            SubtitlePipelinePhase.FAILED,
            SubtitlePipelinePhase.CANCELED,
        ):
            raise ValueError(f"Unsupported terminal state: {terminal_phase}")

        run = self._active_run if self._active_run is not None and self._active_run.run_id == run_id else None
        if run is None:
            logger.debug("Ignoring terminal transition for stale subtitle pipeline run | run_id=%s", run_id)
            return

        log_timing(
            logger,
            "Subtitle timing",
            "job_total",
            elapsed_ms_since(run.started_at),
            run_id=run.run_id,
            media=run.context.media_path,
            output=(run.subtitle_options or run.requested_options).output_path,
            result=terminal_phase.name.lower(),
        )

        is_shutdown = self._service_state == SubtitleServiceState.SHUTTING_DOWN

        self._set_run_phase(run, terminal_phase, f"complete run {run_id}")

        if close_progress:
            self._ui.close_progress_dialog()

        if not self._run_is_waiting_for_thread_cleanup(run):
            self._clear_subtitle_runtime(run)
            self._active_run = None
        if not is_shutdown:
            self._last_result = self._result_from_terminal_phase(terminal_phase)
            self._transition_service_state(
                SubtitleServiceState.IDLE,
                f"record terminal result for run {run_id}",
                allowed=(SubtitleServiceState.IDLE, SubtitleServiceState.DIALOG_OPEN),
            )
        self._release_playback_takeover(resume_playback=not is_shutdown)
        self._complete_shutdown_if_possible()

    def _suspend_player_ui_for_generation(self):
        if self._player_ui_suspend_lease is not None:
            return

        self._player_ui_suspend_lease = self._player.suspend_for_subtitle_generation()

    def _release_playback_takeover(self, *, resume_playback: bool):
        player_ui_suspend_lease = self._player_ui_suspend_lease
        self._player_ui_suspend_lease = None
        if player_ui_suspend_lease is not None:
            player_ui_suspend_lease.release()

        self._playback_takeover.release(resume_playback=resume_playback)

    def _is_current_run_event(self, run_id: int, event_name: str) -> bool:
        if self._active_run is None:
            logger.debug("Ignoring %s for stale subtitle pipeline run | run_id=%s | active_run=<none>", event_name, run_id)
            return False

        if self._active_run.run_id != run_id:
            logger.debug(
                "Ignoring %s for stale subtitle pipeline run | run_id=%s | active_run=%s",
                event_name,
                run_id,
                self._active_run.run_id,
            )
            return False

        return True

    def _require_active_run(self, run_id: int, event_name: str) -> SubtitlePipelineRun | None:
        if not self._is_current_run_event(run_id, event_name):
            return None
        return self._active_run

    def _transition_service_state(
        self,
        new_service_state: SubtitleServiceState,
        reason: str,
        *,
        allowed: tuple[SubtitleServiceState, ...],
    ) -> bool:
        if self._service_state not in allowed:
            logger.warning(
                "Rejected subtitle service state transition | from=%s | to=%s | reason=%s",
                self._service_state.name,
                new_service_state.name,
                reason,
            )
            return False

        if self._service_state != new_service_state:
            logger.debug(
                "Subtitle service state transition | from=%s | to=%s | reason=%s",
                self._service_state.name,
                new_service_state.name,
                reason,
            )
        self._service_state = new_service_state
        return True

    def _result_from_terminal_phase(self, terminal_phase: SubtitlePipelinePhase) -> SubtitlePipelineResult:
        if terminal_phase == SubtitlePipelinePhase.SUCCEEDED:
            return SubtitlePipelineResult.SUCCEEDED
        if terminal_phase == SubtitlePipelinePhase.FAILED:
            return SubtitlePipelineResult.FAILED
        if terminal_phase == SubtitlePipelinePhase.CANCELED:
            return SubtitlePipelineResult.CANCELED
        raise ValueError(f"Unsupported terminal phase: {terminal_phase}")

    def _current_run_id(self) -> int | None:
        if self._active_run is None:
            return None
        return self._active_run.run_id

    def _log_dialog_confirm_timing(self, output_path: str):
        if self._dialog_request_started_at is None:
            return

        log_timing(
            logger,
            "Subtitle timing",
            "dialog_confirm",
            elapsed_ms_since(self._dialog_request_started_at),
            media=self._dialog_request_media_path,
            output=output_path,
        )
        self._clear_dialog_request_timing()

    def _clear_dialog_request_timing(self):
        self._dialog_request_started_at = None
        self._dialog_request_media_path = None

    def _load_generation_audio_tracks_async(self, media_path: str):
        cached_audio_streams = self._get_cached_audio_streams_for_media(media_path)
        if cached_audio_streams is not None:
            logger.debug(
                "Using cached audio stream probe result for generation dialog | media=%s | stream_count=%s",
                media_path,
                len(cached_audio_streams),
            )
            self._apply_loaded_audio_tracks(media_path, cached_audio_streams)
            return

        cached_error = self._get_cached_audio_stream_error_for_media(media_path)
        if cached_error is not None:
            logger.debug(
                "Using cached audio stream probe failure for generation dialog | media=%s | reason=%s",
                media_path,
                cached_error,
            )
            self._apply_audio_track_probe_failure(media_path, cached_error, show_warning=True)
            return

        player_audio_track_count = self._get_player_audio_track_count()
        if player_audio_track_count == 1:
            logger.debug(
                "Skipping audio stream probe for generation dialog because player reports a single audio track | media=%s | player_audio_track_count=%s",
                media_path,
                player_audio_track_count,
            )
            self._cache_audio_stream_probe_success(media_path, [])
            self._apply_default_audio_track_only(media_path)
            return

        self._ui.set_generation_dialog_audio_tracks_loading()
        self._begin_audio_stream_probe(media_path)
        self._audio_stream_probe_request_id += 1
        probe_request_id = self._audio_stream_probe_request_id
        self._current_audio_stream_probe_request_id = probe_request_id

        worker = AudioStreamProbeWorker(probe_request_id, media_path)
        worker.finished.connect(self._on_audio_stream_probe_finished, Qt.QueuedConnection)
        worker.failed.connect(self._on_audio_stream_probe_failed, Qt.QueuedConnection)
        worker.destroyed.connect(lambda *_args, probe_request_id=probe_request_id: self._audio_stream_probe_workers.pop(probe_request_id, None))
        self._audio_stream_probe_workers[probe_request_id] = worker
        worker.start()

    def _invalidate_active_audio_stream_probe_request(self, reason: str):
        if self._current_audio_stream_probe_request_id is None:
            return
        self._abandon_loading_audio_stream_probe()
        logger.debug(
            "Invalidating active audio stream probe request | probe_request_id=%s | reason=%s",
            self._current_audio_stream_probe_request_id,
            reason,
        )
        self._current_audio_stream_probe_request_id = None

    def _get_audio_stream_probe_state(self, media_path: str | None) -> AudioStreamProbeState:
        normalized_media_path = str(media_path or "")
        if not normalized_media_path or self._audio_stream_probe_media_path != normalized_media_path:
            return AudioStreamProbeState.IDLE
        return self._audio_stream_probe_state

    def _get_cached_audio_streams_for_media(self, media_path: str | None):
        if self._get_audio_stream_probe_state(media_path) != AudioStreamProbeState.READY:
            return None
        return self._cached_audio_streams

    def _get_cached_audio_stream_error_for_media(self, media_path: str | None) -> str | None:
        if self._get_audio_stream_probe_state(media_path) != AudioStreamProbeState.FAILED:
            return None
        return self._cached_audio_stream_error

    def _begin_audio_stream_probe(self, media_path: str):
        self._audio_stream_probe_media_path = str(media_path)
        self._audio_stream_probe_state = AudioStreamProbeState.LOADING
        self._cached_audio_streams = None
        self._cached_audio_stream_error = None

    def _abandon_loading_audio_stream_probe(self):
        if self._audio_stream_probe_state != AudioStreamProbeState.LOADING:
            return
        self._audio_stream_probe_media_path = None
        self._audio_stream_probe_state = AudioStreamProbeState.IDLE
        self._cached_audio_streams = None
        self._cached_audio_stream_error = None

    def _cache_audio_stream_probe_success(self, media_path: str, audio_streams):
        self._audio_stream_probe_media_path = str(media_path)
        self._audio_stream_probe_state = AudioStreamProbeState.READY
        self._cached_audio_streams = list(audio_streams)
        self._cached_audio_stream_error = None

    def _cache_audio_stream_probe_failure(self, media_path: str, reason: str):
        self._audio_stream_probe_media_path = str(media_path)
        self._audio_stream_probe_state = AudioStreamProbeState.FAILED
        self._cached_audio_streams = None
        self._cached_audio_stream_error = str(reason).strip() or "Audio stream inspection failed."

    def _get_player_audio_track_count(self) -> int | None:
        try:
            tracks = self._player.get_audio_tracks()
        except (AttributeError, TypeError, ValueError):
            logger.debug("Player audio track list is unavailable for subtitle generation preflight", exc_info=True)
            return None

        try:
            return sum(1 for track_id, _title in tracks if int(track_id) >= 0)
        except (TypeError, ValueError):
            logger.debug("Player audio track list was malformed for subtitle generation preflight", exc_info=True)
            return None

    def _is_current_audio_stream_probe_result(self, probe_request_id: int, media_path: str) -> bool:
        if self._current_audio_stream_probe_request_id != probe_request_id:
            logger.debug(
                "Ignoring stale audio stream probe result because request ownership changed | probe_request_id=%s | active_probe_request_id=%s | media=%s",
                probe_request_id,
                self._current_audio_stream_probe_request_id,
                media_path,
            )
            return False

        if self._service_state != SubtitleServiceState.DIALOG_OPEN:
            logger.debug(
                "Ignoring audio stream probe result because generation dialog is no longer open | probe_request_id=%s | state=%s | media=%s",
                probe_request_id,
                self._service_state.name,
                media_path,
            )
            return False

        if not self._ui.has_generation_dialog():
            logger.debug(
                "Ignoring audio stream probe result because the generation dialog no longer exists | probe_request_id=%s | media=%s",
                probe_request_id,
                media_path,
            )
            return False

        active_media_path = self._dialog_request_media_path or self._player.playback.current_media_path()
        if active_media_path != media_path:
            logger.debug(
                "Ignoring stale audio stream probe result because dialog media changed | probe_request_id=%s | result_media=%s | active_media=%s",
                probe_request_id,
                media_path,
                active_media_path,
            )
            return False

        return True

    def _apply_loaded_audio_tracks(self, media_path: str, audio_streams):
        audio_tracks = self._preflight.build_audio_track_choices(audio_streams)
        selector_enabled = bool(audio_streams)
        self._ui.apply_generation_dialog_audio_tracks(
            audio_tracks,
            selected_track_id=None,
            selector_enabled=selector_enabled,
            generate_enabled=True,
        )
        logger.info(
            "Audio stream probe applied to generation dialog | media=%s | stream_count=%s | selector_enabled=%s",
            media_path,
            len(audio_streams),
            selector_enabled,
        )

    def _apply_default_audio_track_only(self, media_path: str):
        self._ui.apply_generation_dialog_audio_tracks(
            self._preflight.build_audio_track_choices([]),
            selected_track_id=None,
            selector_enabled=False,
            generate_enabled=True,
        )
        logger.debug(
            "Generation dialog using default audio track only | media=%s",
            media_path,
        )

    def _apply_audio_track_probe_failure(self, media_path: str, reason: str, *, show_warning: bool):
        formatted_reason = self._preflight.format_audio_stream_probe_error(reason)
        self._ui.apply_generation_dialog_audio_tracks(
            self._preflight.build_audio_track_choices([]),
            selected_track_id=None,
            selector_enabled=False,
            generate_enabled=True,
        )
        if show_warning:
            show_audio_stream_inspection_warning(self._parent, formatted_reason)
        logger.warning(
            "Audio stream probe left generation dialog in fallback state | media=%s | reason=%s",
            media_path,
            formatted_reason,
        )

    @Slot(int, str, object)
    def _on_audio_stream_probe_finished(self, probe_request_id: int, media_path: str, audio_streams):
        worker = self._audio_stream_probe_workers.pop(probe_request_id, None)
        if worker is not None:
            worker.deleteLater()

        if not self._is_current_audio_stream_probe_result(probe_request_id, media_path):
            return

        self._current_audio_stream_probe_request_id = None
        self._cache_audio_stream_probe_success(media_path, audio_streams)
        self._apply_loaded_audio_tracks(media_path, audio_streams)

    @Slot(int, str, str)
    def _on_audio_stream_probe_failed(self, probe_request_id: int, media_path: str, reason: str):
        worker = self._audio_stream_probe_workers.pop(probe_request_id, None)
        if worker is not None:
            worker.deleteLater()

        if not self._is_current_audio_stream_probe_result(probe_request_id, media_path):
            return

        self._current_audio_stream_probe_request_id = None
        self._cache_audio_stream_probe_failure(media_path, reason)
        self._apply_audio_track_probe_failure(media_path, reason, show_warning=True)
