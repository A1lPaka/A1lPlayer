import logging
import time
from typing import TYPE_CHECKING

from PySide6.QtCore import QCoreApplication, QObject, QThread, Signal, Slot
from PySide6.QtWidgets import QWidget

from models import SubtitleGenerationDialogResult
from services.MediaSettingsStore import MediaSettingsStore
from services.subtitles.SubtitleCudaRuntimeFlow import SubtitleCudaRuntimeFlow
from services.subtitles.SubtitleGenerationAudioProbeFlow import SubtitleGenerationAudioProbeFlow
from services.subtitles.SubtitleGenerationCompletionFlow import SubtitleGenerationCompletionFlow
from services.subtitles.SubtitleGenerationJobRunner import (
    SubtitleGenerationJobRunner,
    can_launch_subtitle_worker_run,
)
from services.subtitles.SubtitleGenerationOutcomePresenter import SubtitleGenerationOutcomePresenter
from services.subtitles.SubtitleGenerationPreflight import SubtitleGenerationPreflight
from services.subtitles.SubtitleGenerationStartFlow import SubtitleGenerationStartFlow
from services.subtitles.SubtitlePipelineState import (
    SubtitlePipelinePhase,
    SubtitlePipelineRun,
    SubtitlePipelineStateMachine,
    SubtitlePipelineTask,
    SubtitleServiceState,
)
from services.subtitles.SubtitleShutdownCoordinator import SubtitleShutdownDecision, SubtitleShutdownCoordinator
from services.subtitles.SubtitleTaskControl import (
    CudaRuntimeTaskControl,
    SubtitleTaskControl,
    SubtitleWorkerTaskControl,
)
from services.subtitles.SubtitleGenerationUiCoordinator import SubtitleGenerationUiCoordinator
from services.subtitles.SubtitleGenerationValidationPresenter import SubtitleGenerationValidationPresenter
from services.subtitles.SubtitleTiming import elapsed_ms_since, log_timing
from ui.MessageBoxService import show_subtitle_generation_already_running
from ui.PlayerWindow import PlayerWindow


logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from services.MediaLibraryService import MediaLibraryService


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
        self._outcome_presenter = SubtitleGenerationOutcomePresenter(parent)
        self._preflight = SubtitleGenerationPreflight(parent)
        self._validation_presenter = SubtitleGenerationValidationPresenter(parent)
        self._pipeline_state = SubtitlePipelineStateMachine()
        self._shutdown = SubtitleShutdownCoordinator(self._pipeline_state)
        self._pending_subtitle_thread_run_ids: set[int] = set()
        self._playback_takeover = self._player.playback.create_interruption_lease(
            self._PLAYBACK_INTERRUPTION_OWNER,
        )
        self._player_ui_suspend_lease = None
        self._dialog_request_started_at: float | None = None
        self._dialog_request_media_path: str | None = None
        self._audio_probe_flow = SubtitleGenerationAudioProbeFlow(
            parent,
            self._player,
            self._ui,
            self._preflight,
            is_generation_dialog_open=self._pipeline_state.has_dialog_open,
            dialog_media_path=lambda: self._dialog_request_media_path,
            dialog_lifecycle_state_name=lambda: self._pipeline_state.dialog_lifecycle_state.name,
        )
        self._audio_probe_flow.thread_finished.connect(self._complete_shutdown_if_possible)
        self._cuda_runtime_flow = SubtitleCudaRuntimeFlow(parent)
        self._completion_flow = SubtitleGenerationCompletionFlow(
            store=self._store,
            media_library=self._media_library,
            ui=self._ui,
            pipeline_state=self._pipeline_state,
            outcome_presenter=self._outcome_presenter,
            complete_run=lambda run_id, phase, close_progress: self._complete_run(
                run_id,
                phase,
                close_progress=close_progress,
            ),
            launch_subtitle_generation=lambda run, options: self._launch_subtitle_generation(run, options),
        )
        self._cuda_runtime_flow.status_changed.connect(self._on_worker_status_changed)
        self._cuda_runtime_flow.details_changed.connect(self._on_worker_details_changed)
        self._cuda_runtime_flow.finished.connect(self._completion_flow.handle_cuda_runtime_install_finished)
        self._cuda_runtime_flow.failed.connect(self._completion_flow.handle_cuda_runtime_install_failed)
        self._cuda_runtime_flow.canceled.connect(self._completion_flow.handle_cuda_runtime_install_canceled)
        self._cuda_runtime_flow.thread_finished.connect(self._on_cuda_runtime_flow_thread_finished)
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
        self._subtitle_job_runner.thread_finished.connect(
            lambda run_id: self._on_background_task_thread_finished(
                run_id,
                SubtitlePipelineTask.SUBTITLE_GENERATION,
            )
        )
        self._task_control_factories = {
            SubtitlePipelineTask.SUBTITLE_GENERATION: self._subtitle_worker_task_control,
            SubtitlePipelineTask.CUDA_INSTALL: self._cuda_runtime_task_control,
        }
        self._cancel_pending_presenters = {
            SubtitlePipelineTask.SUBTITLE_GENERATION: self._ui.show_subtitle_cancel_pending,
            SubtitlePipelineTask.CUDA_INSTALL: self._ui.show_cuda_install_cancel_pending,
        }
        self._start_flow = SubtitleGenerationStartFlow(
            parent=parent,
            player=self._player,
            ui=self._ui,
            preflight=self._preflight,
            validation_presenter=self._validation_presenter,
            audio_probe_flow=self._audio_probe_flow,
            pipeline_state=self._pipeline_state,
            cuda_runtime_flow=self._cuda_runtime_flow,
            outcome_presenter=self._outcome_presenter,
            assert_pipeline_thread=self._assert_pipeline_thread,
            log_dialog_confirm_timing=self._log_dialog_confirm_timing,
            launch_subtitle_generation=lambda run, options: self._launch_subtitle_generation(run, options),
            complete_run=lambda run_id, phase: self._complete_run(run_id, phase, close_progress=True),
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

        if not self._pipeline_state.transition_dialog_lifecycle_state(
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
        self._assert_pipeline_thread()
        if run is not self._pipeline_state.active_job:
            logger.debug("Ignoring subtitle worker launch for stale run | run_id=%s", run.run_id)
            return
        if run.phase not in (SubtitlePipelinePhase.STARTING, SubtitlePipelinePhase.RUNNING):
            logger.warning(
                "Rejected subtitle worker launch because run phase is not launchable | run_id=%s | phase=%s",
                run.run_id,
                run.phase.name,
            )
            return

        self._pipeline_state.set_run_phase(run, SubtitlePipelinePhase.RUNNING, "launch subtitle generation worker")
        if not self._pipeline_state.is_shutdown_in_progress():
            self._pipeline_state.transition_dialog_lifecycle_state(
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

        self._pending_subtitle_thread_run_ids.add(run.run_id)
        self._subtitle_job_runner.start(run, options)

    def _on_subtitle_worker_start_aborted(self, run_id: int, thread: QThread, worker):
        self._assert_pipeline_thread()
        self._pending_subtitle_thread_run_ids.discard(run_id)
        run = self._pipeline_state.active_job
        if run is not None and run.run_id == run_id and run.subtitle_thread is thread and run.subtitle_worker is worker:
            self._clear_subtitle_runtime(run)
        self._complete_shutdown_if_possible()

    def _can_start_subtitle_worker(self, run_id: int, thread: QThread, worker) -> bool:
        run = self._require_active_job(run_id, "deferred subtitle worker launch")
        if run is None:
            return False

        if run.subtitle_thread is not thread or run.subtitle_worker is not worker:
            logger.debug("Skipping deferred subtitle worker launch because worker references changed | run_id=%s", run_id)
            return False

        if not can_launch_subtitle_worker_run(run, thread, worker):
            logger.debug(
                "Skipping deferred subtitle worker launch because run phase changed | run_id=%s | phase=%s",
                run_id,
                run.phase.name,
            )
            return False

        return True

    def _subtitle_worker_task_control(self, run: SubtitlePipelineRun) -> SubtitleTaskControl:
        return SubtitleWorkerTaskControl(run, self._pending_subtitle_thread_run_ids)

    def _cuda_runtime_task_control(self, run: SubtitlePipelineRun) -> SubtitleTaskControl:
        return CudaRuntimeTaskControl(run, self._cuda_runtime_flow)

    def _active_task_control(self) -> SubtitleTaskControl | None:
        run = self._pipeline_state.active_job
        if run is None:
            return None
        return self._task_control_for_run(run)

    def _task_control_for_run(self, run: SubtitlePipelineRun) -> SubtitleTaskControl | None:
        task_control_factory = self._task_control_factories.get(run.task)
        if task_control_factory is None:
            return None
        return task_control_factory(run)

    def _background_task_is_active(self) -> bool:
        run = self._pipeline_state.active_job
        if run is not None and run.keeps_shutdown_pending():
            return True

        task_control = self._active_task_control()
        return task_control is not None and task_control.is_active()

    def _show_active_task_cancel_pending(self, task: SubtitlePipelineTask):
        presenter = self._cancel_pending_presenters.get(task)
        if presenter is not None:
            presenter()

    @Slot()
    def _request_active_task_stop(self, force: bool = False):
        self._assert_pipeline_thread()
        run = self._pipeline_state.active_job
        if run is None or not run.accepts_stop_requests():
            logger.debug(
                "Active task stop ignored because pipeline is not stoppable | run_phase=%s | force=%s",
                run.phase.name if run is not None else "<none>",
                force,
            )
            return

        if run.task in (SubtitlePipelineTask.NONE, SubtitlePipelineTask.CUDA_PROMPT):
            logger.debug("Active task stop ignored because no pipeline task is active | force=%s", force)
            return

        task_control = self._active_task_control()
        if task_control is None:
            logger.debug("Active task stop ignored because no task control exists | task=%s | force=%s", run.task.name, force)
            return

        self._pipeline_state.set_run_phase(run, SubtitlePipelinePhase.CANCELING, f"request stop for {run.task.name.lower()}")
        if not task_control.request_stop(force=force):
            return

        if not force:
            self._show_active_task_cancel_pending(run.task)

    def has_active_tasks(self) -> bool:
        return self._shutdown.has_active_tasks(
            background_task_active=self._background_task_is_active(),
            audio_probe_active=self._audio_probe_flow.is_active(),
        )

    def is_shutdown_in_progress(self) -> bool:
        return self._shutdown.is_shutdown_in_progress()

    def begin_shutdown(self) -> bool:
        self._assert_pipeline_thread()
        action = self._shutdown.begin_graceful_shutdown()
        if action.decision == SubtitleShutdownDecision.ALREADY_FINISHED:
            logger.debug("Subtitle generation service shutdown requested after completion")
            return False

        if action.decision == SubtitleShutdownDecision.REPEATED_GRACEFUL:
            logger.info(
                "Subtitle generation service shutdown already in progress | force_requested=%s",
                self._shutdown.force_requested,
            )
            return self.has_active_tasks()

        logger.info(
            "Subtitle generation service async shutdown started | state=%s",
            self._pipeline_state.dialog_lifecycle_state.name,
        )
        if action.close_generation_dialog:
            self._ui.close_generation_dialog()
        if action.request_task_stop:
            self._request_active_task_stop(force=action.force_task_stop)
        if action.invalidate_audio_probe:
            self._audio_probe_flow.invalidate_active_request("shutdown")
        self._complete_shutdown_if_possible()
        return self.has_active_tasks()

    def begin_force_shutdown(self) -> bool:
        self._assert_pipeline_thread()
        action = self._shutdown.begin_force_shutdown()
        if action.decision == SubtitleShutdownDecision.ALREADY_FINISHED:
            logger.debug("Subtitle generation service force shutdown requested after completion")
            return False

        if action.close_generation_dialog:
            logger.warning("Subtitle generation service force shutdown requested before graceful shutdown")
            self._ui.close_generation_dialog()

        if action.decision == SubtitleShutdownDecision.REPEATED_FORCE:
            logger.info("Repeated force shutdown request ignored for subtitle generation service")
            return self.has_active_tasks()

        logger.warning("Subtitle generation service async force shutdown started")
        if action.close_progress_dialog:
            self._ui.close_progress_dialog()
        if action.request_task_stop:
            self._request_active_task_stop(force=action.force_task_stop)
        if action.stop_audio_probe:
            self._audio_probe_flow.stop_all("shutdown", force=True)
        self._complete_shutdown_if_possible()
        return self.has_active_tasks()

    def _finalize_shutdown_lifecycle(self):
        self._assert_pipeline_thread()
        self._ui.close_progress_dialog()
        self._pipeline_state.discard_active_job()
        self._audio_probe_flow.stop_all("finalize-shutdown", force=True)
        self._release_playback_takeover(resume_playback=False)
        self._shutdown.mark_finished()

    def _complete_shutdown_if_possible(self):
        if not self._shutdown.is_shutdown_in_progress():
            return

        if not self._shutdown.should_emit_shutdown_finished(
            background_task_active=self._background_task_is_active(),
            audio_probe_active=self._audio_probe_flow.is_active(),
        ):
            active_task = self._pipeline_state.active_job.task.name if self._pipeline_state.active_job is not None else "NONE"
            active_phase = (
                self._pipeline_state.active_job_lifecycle_state.name
                if self._pipeline_state.active_job_lifecycle_state is not None
                else "<none>"
            )
            logger.debug(
                "Subtitle generation service shutdown still waiting for background tasks | task=%s | phase=%s | force_requested=%s",
                active_task,
                active_phase,
                self._shutdown.force_requested,
            )
            return

        self._finalize_shutdown_lifecycle()
        logger.info("Subtitle generation service shutdown finished")
        self.shutdown_finished.emit()

    @Slot()
    def _on_generation_dialog_canceled(self):
        self._assert_pipeline_thread()
        if not self._pipeline_state.has_dialog_open():
            return

        self._audio_probe_flow.invalidate_active_request("dialog closed")
        self._clear_dialog_request_timing()
        logger.info("Subtitle generation dialog closed without launching a job")
        self._pipeline_state.transition_dialog_lifecycle_state(
            SubtitleServiceState.IDLE,
            "close generation dialog",
            allowed=(SubtitleServiceState.DIALOG_OPEN,),
        )
        self._release_playback_takeover(resume_playback=True)

    def _on_background_task_thread_finished(self, run_id: int, task: SubtitlePipelineTask):
        self._assert_pipeline_thread()
        if task == SubtitlePipelineTask.SUBTITLE_GENERATION:
            self._pending_subtitle_thread_run_ids.discard(run_id)

        run = self._pipeline_state.active_job if self._pipeline_state.active_job is not None and self._pipeline_state.active_job.run_id == run_id else None
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
                self._pipeline_state.dialog_lifecycle_state.name,
            )

        if run is not None and self._run_is_terminal(run):
            self._pipeline_state.discard_active_job()
        self._complete_shutdown_if_possible()

    def _on_cuda_runtime_flow_thread_finished(self, run_id: int):
        self._on_background_task_thread_finished(run_id, SubtitlePipelineTask.CUDA_INSTALL)

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

    def _clear_subtitle_runtime(self, run: SubtitlePipelineRun):
        self._assert_pipeline_thread()
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
        task_control = self._task_control_for_run(run)
        return task_control is not None and task_control.is_active()

    def _complete_run(
        self,
        run_id: int,
        terminal_phase: SubtitlePipelinePhase,
        *,
            close_progress: bool,
    ):
        self._assert_pipeline_thread()
        run = self._pipeline_state.active_job if self._pipeline_state.active_job is not None and self._pipeline_state.active_job.run_id == run_id else None
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

        is_shutdown = self._pipeline_state.is_shutdown_in_progress()

        if close_progress:
            self._ui.close_progress_dialog()

        clear_active_job = not self._run_is_waiting_for_thread_cleanup(run)
        if clear_active_job:
            self._clear_subtitle_runtime(run)
        self._pipeline_state.complete_run(
            run,
            terminal_phase,
            clear_active_job=clear_active_job,
            record_result=not is_shutdown,
        )
        if not is_shutdown:
            self._pipeline_state.transition_dialog_lifecycle_state(
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

    def _require_active_job(self, run_id: int, event_name: str) -> SubtitlePipelineRun | None:
        if not self._is_current_run_event(run_id, event_name):
            return None
        return self._pipeline_state.active_job

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

    def _current_run_id(self) -> int | None:
        if self._pipeline_state.active_job is None:
            return None
        return self._pipeline_state.active_job.run_id

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
