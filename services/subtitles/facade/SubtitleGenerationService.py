import logging
import time
from typing import TYPE_CHECKING

from PySide6.QtCore import QCoreApplication, QObject, QThread, Signal, Slot
from PySide6.QtWidgets import QWidget

from models.SubtitleGenerationDialogResult import SubtitleGenerationDialogResult
from services.app.MediaSettingsStore import MediaSettingsStore
from services.subtitles.presentation.SubtitleGenerationOutcomePresenter import SubtitleGenerationOutcomePresenter
from services.subtitles.presentation.SubtitleGenerationUiCoordinator import SubtitleGenerationUiCoordinator
from services.subtitles.presentation.SubtitleGenerationValidationPresenter import SubtitleGenerationValidationPresenter
from services.subtitles.workers.SubtitleCudaRuntimeFlow import SubtitleCudaRuntimeFlow
from services.subtitles.workers.SubtitleWhisperModelFlow import SubtitleWhisperModelFlow
from services.subtitles.workers.SubtitleGenerationAudioProbeFlow import SubtitleGenerationAudioProbeFlow
from services.subtitles.application.SubtitleGenerationCompletionFlow import SubtitleGenerationCompletionFlow
from services.subtitles.workers.SubtitleGenerationJobRunner import SubtitleGenerationJobRunner
from services.subtitles.validation.SubtitleGenerationPreflight import SubtitleGenerationPreflight
from services.subtitles.application.SubtitleGenerationRuntimeCoordinator import SubtitleGenerationRuntimeCoordinator
from services.subtitles.application.SubtitleGenerationStartFlow import SubtitleGenerationStartFlow
from services.subtitles.state.SubtitlePipelineState import (
    SubtitlePipelinePhase,
    SubtitlePipelineRun,
    SubtitlePipelineStateMachine,
    SubtitlePipelineTask,
    SubtitleServiceState,
)
from services.subtitles.state.SubtitlePipelineTransitions import SubtitlePipelineTransitions
from services.subtitles.state.SubtitleShutdownCoordinator import SubtitleShutdownCoordinator
from services.subtitles.domain.SubtitleTiming import elapsed_ms_since, log_timing
from ui.MessageBoxService import show_subtitle_generation_already_running
from ui.PlayerWindow import PlayerWindow


logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from services.media.MediaLibraryService import MediaLibraryService


class SubtitleGenerationService(QObject):
    shutdown_finished = Signal()

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
            theme_color_getter=self._current_theme_color,
        )
        self._outcome_presenter = SubtitleGenerationOutcomePresenter(parent)
        self._preflight = SubtitleGenerationPreflight(parent)
        self._validation_presenter = SubtitleGenerationValidationPresenter(parent)
        self._pipeline_state = SubtitlePipelineStateMachine()
        self._transitions = SubtitlePipelineTransitions(self._pipeline_state)
        self._shutdown = SubtitleShutdownCoordinator(self._pipeline_state, self._transitions)
        self._dialog_request_started_at: float | None = None
        self._dialog_request_media_path: str | None = None
        self._audio_probe_flow = SubtitleGenerationAudioProbeFlow(
            parent,
            self._player,
            self._ui,
            self._preflight,
            is_generation_dialog_open=self._pipeline_state.has_dialog_open,
            dialog_media_path=self._dialog_media_path_for_audio_probe,
            dialog_lifecycle_state_name=self._dialog_lifecycle_state_name,
        )
        self._audio_probe_flow.thread_finished.connect(self._complete_shutdown_if_possible)
        self._cuda_runtime_flow = SubtitleCudaRuntimeFlow(parent)
        self._whisper_model_flow = SubtitleWhisperModelFlow(parent)
        self._completion_flow = SubtitleGenerationCompletionFlow(
            store=self._store,
            media_library=self._media_library,
            ui=self._ui,
            transitions=self._transitions,
            outcome_presenter=self._outcome_presenter,
            complete_run=self._dispatch_complete_run,
            launch_subtitle_generation=self._dispatch_launch_subtitle_generation,
            retry_model_install=lambda run, model_size: self._start_flow.retry_whisper_model_install(run, model_size),
        )
        self._cuda_runtime_flow.status_changed.connect(self._on_worker_status_changed)
        self._cuda_runtime_flow.details_changed.connect(self._on_worker_details_changed)
        self._cuda_runtime_flow.finished.connect(self._completion_flow.handle_cuda_runtime_install_finished)
        self._cuda_runtime_flow.failed.connect(self._completion_flow.handle_cuda_runtime_install_failed)
        self._cuda_runtime_flow.canceled.connect(self._completion_flow.handle_cuda_runtime_install_canceled)
        self._cuda_runtime_flow.thread_finished.connect(self._on_cuda_runtime_flow_thread_finished)
        self._whisper_model_flow.status_changed.connect(self._on_worker_status_changed)
        self._whisper_model_flow.details_changed.connect(self._on_worker_details_changed)
        self._whisper_model_flow.finished.connect(self._completion_flow.handle_model_install_finished)
        self._whisper_model_flow.failed.connect(self._completion_flow.handle_model_install_failed)
        self._whisper_model_flow.canceled.connect(self._completion_flow.handle_model_install_canceled)
        self._whisper_model_flow.thread_finished.connect(self._on_whisper_model_flow_thread_finished)
        self._subtitle_job_runner = SubtitleGenerationJobRunner(
            parent,
            can_start_worker=self._can_start_subtitle_worker,
            on_start_aborted=self._on_subtitle_worker_start_aborted,
            suspend_before_start=self._suspend_player_ui_for_generation,
            on_status_changed=self._on_worker_status_changed_from_worker,
            on_progress_changed=self._on_worker_progress_changed_from_worker,
            on_details_changed=self._on_worker_details_changed_from_worker,
            on_finished=self._on_subtitle_generation_finished_from_worker,
            on_failed=self._on_subtitle_generation_failed_from_worker,
            on_canceled=self._on_subtitle_generation_canceled_from_worker,
        )
        self._subtitle_job_runner.thread_finished.connect(self._on_subtitle_worker_thread_finished)
        self._runtime = SubtitleGenerationRuntimeCoordinator(
            player=self._player,
            ui=self._ui,
            pipeline_state=self._pipeline_state,
            transitions=self._transitions,
            shutdown=self._shutdown,
            audio_probe_flow=self._audio_probe_flow,
            cuda_runtime_flow=self._cuda_runtime_flow,
            whisper_model_flow=self._whisper_model_flow,
            subtitle_job_runner=self._subtitle_job_runner,
            assert_pipeline_thread=self._assert_pipeline_thread,
            on_shutdown_finished=self.shutdown_finished.emit,
        )
        self._start_flow = SubtitleGenerationStartFlow(
            parent=parent,
            player=self._player,
            ui=self._ui,
            preflight=self._preflight,
            validation_presenter=self._validation_presenter,
            audio_probe_flow=self._audio_probe_flow,
            pipeline_state=self._pipeline_state,
            transitions=self._transitions,
            cuda_runtime_flow=self._cuda_runtime_flow,
            whisper_model_flow=self._whisper_model_flow,
            outcome_presenter=self._outcome_presenter,
            assert_pipeline_thread=self._assert_pipeline_thread,
            log_dialog_confirm_timing=self._log_dialog_confirm_timing,
            launch_subtitle_generation=self._dispatch_launch_subtitle_generation,
            complete_run=self._dispatch_complete_run,
            request_active_task_stop=self._request_active_task_stop,
        )

    def generate_subtitle(self) -> bool:
        self._assert_pipeline_thread()
        if not self._player.playback.has_media_loaded():
            logger.info("Subtitle generation requested without a loaded media item")
            return False

        current_media_path = self._player.playback.current_media_path()
        if not current_media_path:
            logger.info("Subtitle generation requested without an active media item")
            return False

        if self._shutdown.completed or self._pipeline_state.is_shutdown_in_progress():
            logger.info("Subtitle generation request ignored because shutdown is in progress")
            return False

        if self._pipeline_state.blocks_new_generation_request():
            self._ui.focus_active_dialog()
            show_subtitle_generation_already_running(self._parent)
            logger.info("Subtitle generation request ignored because another background task is running")
            return False

        if self._pipeline_state.has_dialog_open():
            self._ui.focus_active_dialog()
            logger.info("Subtitle generation request focused the existing generation dialog")
            return False

        if not self._pipeline_state.can_open_generation_dialog():
            logger.warning(
                "Subtitle generation request ignored because dialog lifecycle is not openable | state=%s | job_phase=%s",
                self._pipeline_state.dialog_lifecycle_state.name,
                (
                    self._pipeline_state.active_job_lifecycle_state.name
                    if self._pipeline_state.active_job_lifecycle_state is not None
                    else "<none>"
                ),
            )
            return False

        if not self._transitions.open_generation_dialog():
            return False

        self._runtime.playback_takeover.acquire()
        self._dialog_request_started_at = time.perf_counter()
        self._dialog_request_media_path = current_media_path
        self._ui.open_generation_dialog(
            current_media_path,
            on_generate=self._start_flow.start,
            on_cancel=self._on_generation_dialog_canceled,
        )
        self._audio_probe_flow.load_generation_audio_tracks_async(current_media_path)
        return True

    def _launch_subtitle_generation(
        self,
        run: SubtitlePipelineRun,
        options: SubtitleGenerationDialogResult,
    ):
        self._runtime.launch_subtitle_generation(run, options)

    def _dispatch_launch_subtitle_generation(
        self,
        run: SubtitlePipelineRun,
        options: SubtitleGenerationDialogResult,
    ):
        self._launch_subtitle_generation(run, options)

    def _dispatch_complete_run(
        self,
        run_id: int,
        terminal_phase: SubtitlePipelinePhase,
        close_progress: bool,
    ):
        self._complete_run(run_id, terminal_phase, close_progress=close_progress)

    def _on_subtitle_worker_start_aborted(self, run_id: int, thread: QThread, worker):
        self._runtime.on_subtitle_worker_start_aborted(run_id, thread, worker)

    def _can_start_subtitle_worker(self, run_id: int, thread: QThread, worker) -> bool:
        return self._runtime.can_start_subtitle_worker(run_id, thread, worker)

    @Slot()
    def _request_active_task_stop(self, force: bool = False):
        self._runtime.request_active_task_stop(force=force)

    def has_active_tasks(self) -> bool:
        return self._runtime.has_active_tasks()

    def is_shutdown_in_progress(self) -> bool:
        return self._shutdown.is_shutdown_in_progress()

    def begin_shutdown(self) -> bool:
        return self._runtime.begin_shutdown()

    def begin_force_shutdown(self) -> bool:
        return self._runtime.begin_force_shutdown()

    def _begin_emergency_shutdown(self) -> bool:
        return self._runtime.begin_emergency_shutdown()

    def _complete_shutdown_if_possible(self):
        self._runtime.complete_shutdown_if_possible()

    @Slot()
    def _on_generation_dialog_canceled(self):
        self._assert_pipeline_thread()
        if not self._pipeline_state.has_dialog_open():
            return

        self._audio_probe_flow.invalidate_active_request("dialog closed")
        self._clear_dialog_request_timing()
        logger.info("Subtitle generation dialog closed without launching a job")
        self._transitions.close_generation_dialog("close generation dialog")
        self._runtime.release_playback_takeover(resume_playback=True)

    def _on_background_task_thread_finished(self, run_id: int, task: SubtitlePipelineTask):
        self._runtime.on_background_task_thread_finished(run_id, task)

    def _on_cuda_runtime_flow_thread_finished(self, run_id: int):
        self._on_background_task_thread_finished(run_id, SubtitlePipelineTask.CUDA_INSTALL)

    def _on_whisper_model_flow_thread_finished(self, run_id: int):
        self._on_background_task_thread_finished(run_id, SubtitlePipelineTask.MODEL_INSTALL)

    def _on_subtitle_worker_thread_finished(self, run_id: int):
        self._on_background_task_thread_finished(run_id, SubtitlePipelineTask.SUBTITLE_GENERATION)

    def _current_run_id_for_active_subtitle_worker(self, event_name: str) -> int | None:
        run = self._pipeline_state.active_job
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

    def _forward_active_subtitle_worker_event(self, event_name: str, handler, *args):
        run_id = self._current_run_id_for_active_subtitle_worker(event_name)
        if run_id is None:
            return
        handler(run_id, *args)

    @Slot(str)
    def _on_worker_status_changed_from_worker(self, text: str):
        self._forward_active_subtitle_worker_event("status update", self._on_worker_status_changed, text)

    @Slot(int)
    def _on_worker_progress_changed_from_worker(self, value: int):
        self._forward_active_subtitle_worker_event("progress update", self._on_worker_progress_changed, value)

    @Slot(str)
    def _on_worker_details_changed_from_worker(self, text: str):
        self._forward_active_subtitle_worker_event("details update", self._on_worker_details_changed, text)

    @Slot(str, bool, bool)
    def _on_subtitle_generation_finished_from_worker(self, output_path: str, auto_open: bool, used_fallback_output_path: bool):
        self._forward_active_subtitle_worker_event(
            "subtitle generation finished",
            self._completion_flow.handle_subtitle_generation_finished,
            output_path,
            auto_open,
            used_fallback_output_path,
        )

    @Slot(str, str)
    def _on_subtitle_generation_failed_from_worker(self, error_text: str, diagnostics: str):
        self._forward_active_subtitle_worker_event(
            "subtitle generation failed",
            self._completion_flow.handle_subtitle_generation_failed,
            error_text,
            diagnostics,
        )

    @Slot()
    def _on_subtitle_generation_canceled_from_worker(self):
        self._forward_active_subtitle_worker_event(
            "subtitle generation canceled",
            self._completion_flow.handle_subtitle_generation_canceled,
        )

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

    def _complete_run(
        self,
        run_id: int,
        terminal_phase: SubtitlePipelinePhase,
        *,
            close_progress: bool,
    ):
        self._runtime.complete_run(run_id, terminal_phase, close_progress=close_progress)

    def _suspend_player_ui_for_generation(self):
        self._runtime.suspend_player_ui_for_generation()

    def _release_playback_takeover(self, *, resume_playback: bool):
        self._runtime.release_playback_takeover(resume_playback=resume_playback)

    def _is_current_run_event(self, run_id: int, event_name: str) -> bool:
        if self._pipeline_state.active_job is None:
            logger.debug("Ignoring %s for stale subtitle pipeline run | run_id=%s | active_job=<none>", event_name, run_id)
            return False

        if self._pipeline_state.active_job.run_id != run_id:
            logger.debug(
                "Ignoring %s for stale subtitle pipeline run | run_id=%s | active_job=%s",
                event_name,
                run_id,
                self._pipeline_state.active_job.run_id,
            )
            return False

        return True

    def _assert_pipeline_thread(self):
        app = QCoreApplication.instance()
        if app is None:
            return

        if self._is_pipeline_thread(
            is_main_thread=QThread.isMainThread(),
            service_thread=self.thread(),
            app_thread=app.thread(),
        ):
            return

        message = "SubtitleGenerationService pipeline state must be mutated from its Qt owner thread"
        logger.critical(message)
        raise RuntimeError(message)

    @staticmethod
    def _is_pipeline_thread(*, is_main_thread: bool, service_thread, app_thread) -> bool:
        return is_main_thread and service_thread == app_thread

    @property
    def _pending_subtitle_thread_run_ids(self):
        return self._runtime.pending_subtitle_thread_run_ids

    @property
    def _playback_takeover(self):
        return self._runtime.playback_takeover

    @property
    def _player_ui_suspend_lease(self):
        return self._runtime.player_ui_suspend_lease

    def _current_theme_color(self):
        return self._player.theme_color

    def _dialog_media_path_for_audio_probe(self) -> str | None:
        return self._dialog_request_media_path

    def _dialog_lifecycle_state_name(self) -> str:
        return self._pipeline_state.dialog_lifecycle_state.name

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
