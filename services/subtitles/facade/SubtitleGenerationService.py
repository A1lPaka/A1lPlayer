import logging
import time
from typing import TYPE_CHECKING

from PySide6.QtCore import QCoreApplication, QObject, QThread, Signal, Slot
from PySide6.QtWidgets import QWidget

from models.SubtitleGenerationDialogResult import SubtitleGenerationDialogResult
from services.app.MediaSettingsStore import MediaSettingsStore
from services.subtitles.composition.SubtitleGenerationComposition import (
    SubtitleGenerationComposition,
    SubtitleGenerationCompositionCallbacks,
    SubtitleGenerationDialogCallbacks,
    SubtitleGenerationPipelineCallbacks,
    SubtitleGenerationShutdownCallbacks,
    SubtitleGenerationWorkerCallbacks,
)
from services.subtitles.state.SubtitlePipelineState import (
    SubtitlePipelinePhase,
    SubtitlePipelineRun,
    SubtitlePipelineTask,
)
from services.subtitles.workers.SubtitleGenerationJobRunner import (
    SubtitleWorkerEventCallbacks,
    SubtitleWorkerLaunchCallbacks,
)
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
        self._dialog_request_started_at: float | None = None
        self._dialog_request_media_path: str | None = None
        composition = SubtitleGenerationComposition.create(
            parent=parent,
            player=self._player,
            store=self._store,
            media_library=self._media_library,
            callbacks=self._composition_callbacks(),
        )
        self._ui = composition.ui
        self._outcome_presenter = composition.outcome_presenter
        self._preflight = composition.preflight
        self._validation_presenter = composition.validation_presenter
        self._pipeline_state = composition.pipeline_state
        self._transitions = composition.transitions
        self._shutdown = composition.shutdown
        self._audio_probe_flow = composition.audio_probe_flow
        self._cuda_runtime_flow = composition.cuda_runtime_flow
        self._whisper_model_flow = composition.whisper_model_flow
        self._completion_flow = composition.completion_flow
        self._subtitle_job_runner = composition.subtitle_job_runner
        self._runtime = composition.runtime
        self._start_flow = composition.start_flow

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

    def _composition_callbacks(self) -> SubtitleGenerationCompositionCallbacks:
        return SubtitleGenerationCompositionCallbacks(
            dialog=SubtitleGenerationDialogCallbacks(
                current_theme_color=self._current_theme_color,
                dialog_media_path_for_audio_probe=self._dialog_media_path_for_audio_probe,
                dialog_lifecycle_state_name=self._dialog_lifecycle_state_name,
                log_dialog_confirm_timing=self._log_dialog_confirm_timing,
            ),
            pipeline=SubtitleGenerationPipelineCallbacks(
                assert_pipeline_thread=self._assert_pipeline_thread,
                complete_run=self._dispatch_complete_run,
                launch_subtitle_generation=self._dispatch_launch_subtitle_generation,
                retry_whisper_model_install=self._retry_whisper_model_install,
                request_active_task_stop=self._request_active_task_stop,
            ),
            worker=SubtitleGenerationWorkerCallbacks(
                subtitle_launch=SubtitleWorkerLaunchCallbacks(
                    can_start_worker=self._can_start_subtitle_worker,
                    on_start_aborted=self._on_subtitle_worker_start_aborted,
                    suspend_before_start=self._suspend_player_ui_for_generation,
                ),
                subtitle_events=SubtitleWorkerEventCallbacks(
                    on_status_changed=self._on_worker_status_changed_from_worker,
                    on_progress_changed=self._on_worker_progress_changed_from_worker,
                    on_details_changed=self._on_worker_details_changed_from_worker,
                    on_finished=self._on_subtitle_generation_finished_from_worker,
                    on_failed=self._on_subtitle_generation_failed_from_worker,
                    on_canceled=self._on_subtitle_generation_canceled_from_worker,
                ),
                on_subtitle_worker_thread_finished=self._on_subtitle_worker_thread_finished,
                on_worker_status_changed=self._on_worker_status_changed,
                on_worker_details_changed=self._on_worker_details_changed,
            ),
            shutdown=SubtitleGenerationShutdownCallbacks(
                emit_shutdown_finished=self.shutdown_finished.emit,
                complete_shutdown_if_possible=self._complete_shutdown_if_possible,
                on_cuda_runtime_flow_thread_finished=self._on_cuda_runtime_flow_thread_finished,
                on_whisper_model_flow_thread_finished=self._on_whisper_model_flow_thread_finished,
            ),
        )

    def _retry_whisper_model_install(self, run: SubtitlePipelineRun, model_size: str):
        self._start_flow.retry_whisper_model_install(run, model_size)

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

    def begin_emergency_shutdown(self) -> bool:
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

    def _forward_active_subtitle_worker_event(
        self,
        event_name: str,
        run_id: int,
        worker,
        handler,
        *args,
        terminal: bool = False,
    ):
        self._runtime.forward_active_subtitle_worker_event(
            event_name,
            run_id,
            worker,
            handler,
            *args,
            terminal=terminal,
        )

    def _on_worker_status_changed_from_worker(self, run_id: int, worker, text: str):
        self._forward_active_subtitle_worker_event("status update", run_id, worker, self._on_worker_status_changed, text)

    def _on_worker_progress_changed_from_worker(self, run_id: int, worker, value: int):
        self._forward_active_subtitle_worker_event("progress update", run_id, worker, self._on_worker_progress_changed, value)

    def _on_worker_details_changed_from_worker(self, run_id: int, worker, text: str):
        self._forward_active_subtitle_worker_event("details update", run_id, worker, self._on_worker_details_changed, text)

    def _on_subtitle_generation_finished_from_worker(
        self,
        run_id: int,
        worker,
        output_path: str,
        auto_open: bool,
        used_fallback_output_path: bool,
    ):
        self._forward_active_subtitle_worker_event(
            "subtitle generation finished",
            run_id,
            worker,
            self._completion_flow.handle_subtitle_generation_finished,
            output_path,
            auto_open,
            used_fallback_output_path,
            terminal=True,
        )

    def _on_subtitle_generation_failed_from_worker(self, run_id: int, worker, error_text: str, diagnostics: str):
        self._forward_active_subtitle_worker_event(
            "subtitle generation failed",
            run_id,
            worker,
            self._completion_flow.handle_subtitle_generation_failed,
            error_text,
            diagnostics,
            terminal=True,
        )

    def _on_subtitle_generation_canceled_from_worker(self, run_id: int, worker):
        self._forward_active_subtitle_worker_event(
            "subtitle generation canceled",
            run_id,
            worker,
            self._completion_flow.handle_subtitle_generation_canceled,
            terminal=True,
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
