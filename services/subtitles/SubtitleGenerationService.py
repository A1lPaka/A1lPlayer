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
from services.subtitles.SubtitleGenerationOutcomeHandler import (
    SubtitleAutoOpenOutcome,
    SubtitleGenerationOutcomeHandler,
)
from services.subtitles.SubtitleGenerationPreflight import AudioStreamProbeState, SubtitleGenerationPreflight
from services.subtitles.SubtitleGenerationUiCoordinator import SubtitleGenerationUiCoordinator
from services.subtitles.SubtitleGenerationWorkers import AudioStreamProbeWorker, SubtitleGenerationWorker
from services.subtitles.SubtitleMaker import (
    get_missing_windows_cuda_runtime_packages,
)
from services.subtitles.SubtitleTiming import elapsed_ms_since, log_timing
from ui.MessageBoxService import (
    prompt_cuda_runtime_choice,
    show_audio_stream_inspection_warning,
    show_subtitle_generation_already_running,
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


class SubtitleGenerationState(Enum):
    IDLE = auto()
    DIALOG_OPEN = auto()
    STARTING = auto()
    RUNNING = auto()
    CANCELING = auto()
    SHUTTING_DOWN = auto()
    SUCCEEDED = auto()
    FAILED = auto()
    CANCELED = auto()


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
    task: SubtitlePipelineTask = SubtitlePipelineTask.NONE
    started_at: float = field(default_factory=time.perf_counter)


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
        self._outcomes = SubtitleGenerationOutcomeHandler(parent)
        self._state = SubtitleGenerationState.IDLE
        self._active_run: SubtitlePipelineRun | None = None
        self._next_run_id = 1
        self._shutdown_completed = False
        self._force_shutdown_requested = False
        self._player_ui_suspended = False
        self._subtitle_thread: QThread | None = None
        self._subtitle_worker: SubtitleGenerationWorker | None = None
        self._subtitle_cancel_requested = False
        self._audio_stream_probe_media_path: str | None = None
        self._audio_stream_probe_state = AudioStreamProbeState.IDLE
        self._cached_audio_streams = None
        self._cached_audio_stream_error: str | None = None
        self._audio_stream_probe_request_id = 0
        self._current_audio_stream_probe_request_id: int | None = None
        self._audio_stream_probe_workers: dict[int, AudioStreamProbeWorker] = {}
        self._dialog_request_started_at: float | None = None
        self._dialog_request_media_path: str | None = None
        self._cuda_runtime_flow = SubtitleCudaRuntimeFlow(parent, self._ui)
        self._cuda_runtime_flow.status_changed.connect(self._on_cuda_flow_status_changed)
        self._cuda_runtime_flow.details_changed.connect(self._on_cuda_flow_details_changed)
        self._cuda_runtime_flow.finished.connect(self._on_cuda_runtime_install_finished)
        self._cuda_runtime_flow.failed.connect(self._on_cuda_runtime_install_failed)
        self._cuda_runtime_flow.canceled.connect(self._on_cuda_runtime_install_canceled)
        self._cuda_runtime_flow.thread_finished.connect(self._on_cuda_runtime_flow_thread_finished)

    def generate_subtitle(self) -> bool:
        current_media_path = self._player.playback.current_media_path()
        if not current_media_path:
            logger.info("Subtitle generation requested without an active media item")
            return False

        if self._shutdown_completed or self._state == SubtitleGenerationState.SHUTTING_DOWN:
            logger.info("Subtitle generation request ignored because shutdown is in progress")
            return False

        if self._state == SubtitleGenerationState.DIALOG_OPEN:
            self._ui.focus_active_dialog()
            logger.info("Subtitle generation request focused the existing generation dialog")
            return False

        if self.has_active_tasks() or self._state in (
            SubtitleGenerationState.STARTING,
            SubtitleGenerationState.RUNNING,
            SubtitleGenerationState.CANCELING,
        ):
            self._ui.focus_active_dialog()
            show_subtitle_generation_already_running(self._parent)
            logger.info("Subtitle generation request ignored because another background task is running")
            return False

        if not self._transition_state(
            SubtitleGenerationState.DIALOG_OPEN,
            "open generation dialog",
            allowed=(
                SubtitleGenerationState.IDLE,
                SubtitleGenerationState.SUCCEEDED,
                SubtitleGenerationState.FAILED,
                SubtitleGenerationState.CANCELED,
            ),
        ):
            return False

        self._pause_for_generation_dialog_if_needed()
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
        if not self._transition_state(
            SubtitleGenerationState.STARTING,
            "start subtitle generation",
            allowed=(SubtitleGenerationState.DIALOG_OPEN,),
        ):
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
            if self._state == SubtitleGenerationState.STARTING:
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
        if not self._transition_state(
            SubtitleGenerationState.RUNNING,
            "launch subtitle generation worker",
            allowed=(SubtitleGenerationState.STARTING, SubtitleGenerationState.RUNNING),
        ):
            return

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
        self._ui.open_generation_progress(options, on_cancel=self._cancel_subtitle_generation)

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

        self._subtitle_thread = thread
        self._subtitle_worker = worker
        self._subtitle_cancel_requested = False
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
        if not self._transition_state(
            SubtitleGenerationState.RUNNING,
            "start CUDA runtime install",
            allowed=(SubtitleGenerationState.STARTING,),
        ):
            return

        logger.info(
            "Starting CUDA runtime install flow | run_id=%s | media=%s | request_id=%s | packages=%s",
            run.run_id,
            run.context.media_path,
            run.context.request_id,
            ", ".join(missing_packages),
        )
        run.task = SubtitlePipelineTask.CUDA_INSTALL
        if not self._cuda_runtime_flow.start(run.run_id, missing_packages):
            logger.error("CUDA runtime install flow could not be started | run_id=%s", run.run_id)
            self._complete_run(
                run.run_id,
                SubtitleGenerationState.FAILED,
                close_progress=True,
            )
            if self._state != SubtitleGenerationState.SHUTTING_DOWN:
                self._outcomes.show_cuda_install_failed("GPU runtime installation could not be started.")
            return

        self._pause_for_generation_dialog_if_needed()

    def _deferred_suspend_and_start_subtitle_worker(self, run_id: int, thread: QThread):
        if not self._is_current_run_event(run_id, "deferred subtitle worker launch"):
            logger.debug("Skipping deferred subtitle worker launch for stale run | run_id=%s", run_id)
            return

        if self._subtitle_thread is not thread or self._subtitle_worker is None:
            logger.debug("Skipping deferred subtitle worker launch because worker references changed | run_id=%s", run_id)
            return

        if self._state not in (
            SubtitleGenerationState.RUNNING,
            SubtitleGenerationState.CANCELING,
            SubtitleGenerationState.SHUTTING_DOWN,
        ):
            logger.debug(
                "Skipping deferred subtitle worker launch because pipeline state changed | run_id=%s | state=%s",
                run_id,
                self._state.name,
            )
            return

        self._pause_for_generation_dialog_if_needed()
        self._ensure_player_ui_suspended()
        QTimer.singleShot(
            0,
            lambda run_id=run_id, thread=thread: self._deferred_start_subtitle_worker(run_id, thread),
        )

    def _deferred_start_subtitle_worker(self, run_id: int, thread: QThread):
        if not self._is_current_run_event(run_id, "deferred subtitle worker thread start"):
            logger.debug("Skipping deferred subtitle worker thread start for stale run | run_id=%s", run_id)
            return

        if self._subtitle_thread is not thread or self._subtitle_worker is None:
            logger.debug("Skipping deferred subtitle worker thread start because worker references changed | run_id=%s", run_id)
            return

        if thread.isRunning():
            logger.debug("Skipping deferred subtitle worker thread start because thread is already running | run_id=%s", run_id)
            return

        thread.start()

    @Slot()
    def _cancel_subtitle_generation(self):
        if self._state not in (SubtitleGenerationState.RUNNING, SubtitleGenerationState.CANCELING):
            logger.debug("Subtitle generation cancel ignored because pipeline is not running | state=%s", self._state.name)
            return

        if self._subtitle_worker is None:
            logger.debug("Subtitle generation cancel ignored because no subtitle worker is active")
            return

        if self._subtitle_cancel_requested:
            logger.info("Repeated cancel request ignored for subtitle generation worker")
            return

        self._transition_state(
            SubtitleGenerationState.CANCELING,
            "cancel subtitle generation",
            allowed=(SubtitleGenerationState.RUNNING, SubtitleGenerationState.CANCELING),
        )
        self._subtitle_cancel_requested = True
        logger.info("Cancel requested for subtitle generation worker | run_id=%s", self._current_run_id())
        self._subtitle_worker.cancel()
        self._ui.show_subtitle_cancel_pending()

    @Slot()
    def _cancel_cuda_runtime_install(self):
        if self._state not in (SubtitleGenerationState.RUNNING, SubtitleGenerationState.CANCELING):
            logger.debug("CUDA runtime install cancel ignored because pipeline is not running | state=%s", self._state.name)
            return

        if not self._cuda_runtime_flow.is_active():
            logger.debug("CUDA runtime install cancel ignored because no CUDA flow is active")
            return

        self._transition_state(
            SubtitleGenerationState.CANCELING,
            "cancel CUDA runtime install",
            allowed=(SubtitleGenerationState.RUNNING, SubtitleGenerationState.CANCELING),
        )
        logger.info("Cancel requested for CUDA runtime install worker | run_id=%s", self._current_run_id())
        self._cuda_runtime_flow.cancel()

    def has_active_tasks(self) -> bool:
        return self._is_thread_active(self._subtitle_thread) or self._cuda_runtime_flow.is_active()

    def is_shutdown_in_progress(self) -> bool:
        return self._state == SubtitleGenerationState.SHUTTING_DOWN and not self._shutdown_completed

    def begin_shutdown(self) -> bool:
        if self._shutdown_completed:
            logger.debug("Subtitle generation service shutdown requested after completion")
            return False

        if self._state == SubtitleGenerationState.SHUTTING_DOWN:
            logger.info(
                "Subtitle generation service shutdown already in progress | force_requested=%s",
                self._force_shutdown_requested,
            )
            return self.has_active_tasks()

        logger.info("Subtitle generation service async shutdown started | state=%s", self._state.name)
        self._transition_state(
            SubtitleGenerationState.SHUTTING_DOWN,
            "begin graceful shutdown",
            allowed=tuple(SubtitleGenerationState),
        )
        self._force_shutdown_requested = False
        self._ui.close_generation_dialog()
        self._request_background_task_stop(force=False)
        self._complete_shutdown_if_possible()
        return self.has_active_tasks()

    def begin_force_shutdown(self) -> bool:
        if self._shutdown_completed:
            logger.debug("Subtitle generation service force shutdown requested after completion")
            return False

        if self._state != SubtitleGenerationState.SHUTTING_DOWN:
            logger.warning("Subtitle generation service force shutdown requested before graceful shutdown")
            self._transition_state(
                SubtitleGenerationState.SHUTTING_DOWN,
                "begin force shutdown",
                allowed=tuple(SubtitleGenerationState),
            )
            self._ui.close_generation_dialog()

        if self._force_shutdown_requested:
            logger.info("Repeated force shutdown request ignored for subtitle generation service")
            return self.has_active_tasks()

        logger.warning("Subtitle generation service async force shutdown started")
        self._force_shutdown_requested = True
        self._ui.close_progress_dialog()
        self._request_background_task_stop(force=True)
        self._complete_shutdown_if_possible()
        return self.has_active_tasks()

    def _request_background_task_stop(self, force: bool):
        if self._subtitle_worker is not None:
            if force:
                self._subtitle_worker.force_stop()
            elif not self._subtitle_cancel_requested:
                self._subtitle_cancel_requested = True
                logger.info("Cancel requested for subtitle generation worker during shutdown | run_id=%s", self._current_run_id())
                self._subtitle_worker.cancel()
                self._ui.show_subtitle_cancel_pending()

        if self._cuda_runtime_flow.is_active():
            logger.info(
                "Stop requested for CUDA runtime install flow during shutdown | run_id=%s | force=%s",
                self._current_run_id(),
                force,
            )
            self._cuda_runtime_flow.request_stop(force=force)

        self._invalidate_active_audio_stream_probe_request("shutdown")

    def _finalize_shutdown_state(self):
        self._ui.close_progress_dialog()
        self._active_run = None
        self._clear_subtitle_thread_references()
        self._invalidate_active_audio_stream_probe_request("finalize-shutdown")
        self._player.playback.clear_interruption(self._PLAYBACK_INTERRUPTION_OWNER)
        self._ensure_player_ui_resumed()
        self._shutdown_completed = True
        self._force_shutdown_requested = False

    def _is_thread_active(self, thread: QThread | None) -> bool:
        return thread is not None and thread.isRunning()

    def _complete_shutdown_if_possible(self):
        if self._state != SubtitleGenerationState.SHUTTING_DOWN or self._shutdown_completed:
            return

        if self.has_active_tasks():
            logger.debug(
                "Subtitle generation service shutdown still waiting for background tasks | subtitle_running=%s | cuda_running=%s | force_requested=%s",
                self._is_thread_active(self._subtitle_thread),
                self._cuda_runtime_flow.is_active(),
                self._force_shutdown_requested,
            )
            return

        self._finalize_shutdown_state()
        logger.info("Subtitle generation service shutdown finished")
        self.shutdown_finished.emit()

    @Slot()
    def _on_generation_dialog_canceled(self):
        if self._state != SubtitleGenerationState.DIALOG_OPEN:
            return

        self._invalidate_active_audio_stream_probe_request("dialog closed")
        self._clear_dialog_request_timing()
        logger.info("Subtitle generation dialog closed without launching a job")
        self._transition_state(
            SubtitleGenerationState.IDLE,
            "close generation dialog",
            allowed=(SubtitleGenerationState.DIALOG_OPEN,),
        )
        self._resume_after_generation_dialog_if_needed()

    def _on_background_task_thread_finished(self, run_id: int, task: SubtitlePipelineTask):
        if task == SubtitlePipelineTask.SUBTITLE_GENERATION:
            self._clear_subtitle_thread_references()

        if self._active_run is not None and self._active_run.run_id == run_id:
            logger.debug(
                "Background task thread finished for active subtitle pipeline run | run_id=%s | task=%s | state=%s",
                run_id,
                task.name,
                self._state.name,
            )
        else:
            logger.debug(
                "Background task thread finished for stale subtitle pipeline run | run_id=%s | task=%s | state=%s",
                run_id,
                task.name,
                self._state.name,
            )

        self._complete_shutdown_if_possible()

    def _on_cuda_runtime_flow_thread_finished(self, run_id: int):
        self._on_background_task_thread_finished(run_id, SubtitlePipelineTask.CUDA_INSTALL)

    def _current_run_id_for_active_subtitle_worker(self, event_name: str) -> int | None:
        if self._subtitle_worker is None:
            logger.debug("Ignoring %s because no subtitle worker is active", event_name)
            return None

        sender = self.sender()
        if sender is not self._subtitle_worker:
            logger.debug(
                "Ignoring %s from stale subtitle worker | sender_matches_active=%s",
                event_name,
                sender is self._subtitle_worker,
            )
            return None

        run_id = self._current_run_id()
        if run_id is None:
            logger.debug("Ignoring %s because subtitle run ownership is already gone", event_name)
            return None
        return run_id

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

    def _on_cuda_flow_status_changed(self, run_id: int, text: str):
        self._on_worker_status_changed(run_id, text)

    def _on_worker_progress_changed(self, run_id: int, value: int):
        if not self._is_current_run_event(run_id, "progress update"):
            return
        self._ui.update_progress(value)

    def _on_worker_details_changed(self, run_id: int, text: str):
        if not self._is_current_run_event(run_id, "details update"):
            return
        self._ui.update_progress_details(text)

    def _on_cuda_flow_details_changed(self, run_id: int, text: str):
        self._on_worker_details_changed(run_id, text)

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
            SubtitleGenerationState.SUCCEEDED,
            close_progress=True,
        )

        if self._state == SubtitleGenerationState.SHUTTING_DOWN:
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

        self._outcomes.show_generation_success(
            output_path,
            auto_open_outcome,
            used_fallback_output_path=used_fallback_output_path,
            requested_output_path=(run.subtitle_options or run.requested_options).output_path,
        )
        self._resume_after_generation_dialog_if_needed()

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
            SubtitleGenerationState.FAILED,
            close_progress=True,
        )

        if self._state == SubtitleGenerationState.SHUTTING_DOWN:
            return

        self._outcomes.show_generation_failed(error_text)
        self._resume_after_generation_dialog_if_needed()

    def _on_subtitle_generation_canceled(self, run_id: int):
        run = self._require_active_run(run_id, "subtitle generation canceled")
        if run is None:
            return

        logger.info("Subtitle generation canceled | run_id=%s", run.run_id)
        self._complete_run(
            run_id,
            SubtitleGenerationState.CANCELED,
            close_progress=True,
        )

        if self._state == SubtitleGenerationState.SHUTTING_DOWN:
            return

        self._outcomes.show_generation_canceled()
        self._resume_after_generation_dialog_if_needed()

    def _on_cuda_runtime_install_finished(self, run_id: int):
        run = self._require_active_run(run_id, "CUDA runtime install finished")
        if run is None:
            return

        logger.info("CUDA runtime install flow finished | run_id=%s", run.run_id)
        self._ui.close_progress_dialog()

        if self._state == SubtitleGenerationState.CANCELING:
            logger.info("Ignoring CUDA runtime completion because pipeline cancellation is already in progress | run_id=%s", run.run_id)
            self._complete_run(
                run_id,
                SubtitleGenerationState.CANCELED,
                close_progress=False,
            )
            return

        if self._state == SubtitleGenerationState.SHUTTING_DOWN:
            self._complete_run(
                run_id,
                SubtitleGenerationState.CANCELED,
                close_progress=False,
            )
            return

        if run.subtitle_options is None:
            logger.warning("CUDA runtime install finished without pending subtitle generation options | run_id=%s", run.run_id)
            self._complete_run(
                run_id,
                SubtitleGenerationState.FAILED,
                close_progress=False,
            )
            self._outcomes.show_cuda_install_failed("GPU runtime installation finished without subtitle options.")
            self._resume_after_generation_dialog_if_needed()
            return

        self._launch_subtitle_generation(run, run.subtitle_options)

    def _on_cuda_runtime_install_failed(self, run_id: int, error_text: str):
        run = self._require_active_run(run_id, "CUDA runtime install failed")
        if run is None:
            return

        logger.error("CUDA runtime install failed | run_id=%s | message=%s", run.run_id, error_text)
        self._complete_run(
            run_id,
            SubtitleGenerationState.FAILED,
            close_progress=True,
        )

        if self._state == SubtitleGenerationState.SHUTTING_DOWN:
            return

        self._outcomes.show_cuda_install_failed(error_text)
        self._resume_after_generation_dialog_if_needed()

    def _on_cuda_runtime_install_canceled(self, run_id: int):
        run = self._require_active_run(run_id, "CUDA runtime install canceled")
        if run is None:
            return

        logger.info("CUDA runtime install canceled | run_id=%s", run.run_id)
        self._complete_run(
            run_id,
            SubtitleGenerationState.CANCELED,
            close_progress=True,
        )

        if self._state == SubtitleGenerationState.SHUTTING_DOWN:
            return

        self._outcomes.show_cuda_install_canceled()
        self._resume_after_generation_dialog_if_needed()

    @Slot()
    def _clear_subtitle_thread_references(self, *_args):
        self._subtitle_thread = None
        self._subtitle_worker = None
        self._subtitle_cancel_requested = False

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
        if self._active_run is not None:
            logger.debug("Discarding starting subtitle pipeline run | run_id=%s | reason=%s", self._active_run.run_id, reason)
        self._active_run = None
        self._transition_back_to_dialog(reason)

    def _transition_back_to_dialog(self, reason: str):
        self._transition_state(
            SubtitleGenerationState.DIALOG_OPEN,
            reason,
            allowed=(SubtitleGenerationState.STARTING, SubtitleGenerationState.DIALOG_OPEN),
        )

    def _complete_run(
        self,
        run_id: int,
        terminal_state: SubtitleGenerationState,
        *,
        close_progress: bool,
    ):
        if terminal_state not in (
            SubtitleGenerationState.SUCCEEDED,
            SubtitleGenerationState.FAILED,
            SubtitleGenerationState.CANCELED,
        ):
            raise ValueError(f"Unsupported terminal state: {terminal_state}")

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
            result=terminal_state.name.lower(),
        )

        previous_state = self._state
        is_shutdown = previous_state == SubtitleGenerationState.SHUTTING_DOWN

        if not is_shutdown:
            self._transition_state(
                terminal_state,
                f"complete run {run_id}",
                allowed=(
                    SubtitleGenerationState.RUNNING,
                    SubtitleGenerationState.CANCELING,
                    SubtitleGenerationState.STARTING,
                ),
            )

        if close_progress:
            self._ui.close_progress_dialog()

        self._active_run = None
        self._ensure_player_ui_resumed()
        self._complete_shutdown_if_possible()

    def _ensure_player_ui_suspended(self):
        if self._player_ui_suspended:
            return
        self._player.suspend_for_subtitle_generation()
        self._player_ui_suspended = True

    def _ensure_player_ui_resumed(self):
        if not self._player_ui_suspended:
            return
        self._player.resume_after_subtitle_generation()
        self._player_ui_suspended = False

    def _pause_for_generation_dialog_if_needed(self):
        if self._player.playback.pause_for_interruption(self._PLAYBACK_INTERRUPTION_OWNER):
            self._player.playback.pause()

    def _resume_after_generation_dialog_if_needed(self):
        self._player.playback.resume_after_interruption(self._PLAYBACK_INTERRUPTION_OWNER)

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

    def _transition_state(
        self,
        new_state: SubtitleGenerationState,
        reason: str,
        *,
        allowed: tuple[SubtitleGenerationState, ...],
    ) -> bool:
        if self._state not in allowed:
            logger.warning(
                "Rejected subtitle pipeline state transition | from=%s | to=%s | reason=%s",
                self._state.name,
                new_state.name,
                reason,
            )
            return False

        if self._state != new_state:
            logger.debug(
                "Subtitle pipeline state transition | from=%s | to=%s | reason=%s",
                self._state.name,
                new_state.name,
                reason,
            )
        self._state = new_state
        return True

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
            self._apply_audio_track_probe_failure(media_path, cached_error, show_warning=False)
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

    def _is_current_audio_stream_probe_result(self, probe_request_id: int, media_path: str) -> bool:
        if self._current_audio_stream_probe_request_id != probe_request_id:
            logger.debug(
                "Ignoring stale audio stream probe result because request ownership changed | probe_request_id=%s | active_probe_request_id=%s | media=%s",
                probe_request_id,
                self._current_audio_stream_probe_request_id,
                media_path,
            )
            return False

        if self._state != SubtitleGenerationState.DIALOG_OPEN:
            logger.debug(
                "Ignoring audio stream probe result because generation dialog is no longer open | probe_request_id=%s | state=%s | media=%s",
                probe_request_id,
                self._state.name,
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
