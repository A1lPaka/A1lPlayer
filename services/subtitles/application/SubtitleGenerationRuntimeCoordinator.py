from __future__ import annotations

import logging
from collections.abc import Callable

from services.subtitles.workers.SubtitleGenerationJobRunner import (
    SubtitleGenerationJobRunner,
    can_launch_subtitle_worker_run,
)
from services.subtitles.state.SubtitlePipelineState import (
    SubtitlePipelinePhase,
    SubtitlePipelineRun,
    SubtitlePipelineStateMachine,
    SubtitlePipelineTask,
)
from services.subtitles.state.SubtitlePipelineTransitions import SubtitlePipelineTransitions
from services.subtitles.state.SubtitleShutdownCoordinator import (
    SubtitleShutdownAction,
    SubtitleShutdownCoordinator,
    SubtitleShutdownDecision,
)
from services.subtitles.application.SubtitleTaskControl import (
    CudaRuntimeTaskControl,
    SubtitleTaskControl,
    SubtitleWorkerTaskControl,
    WhisperModelTaskControl,
)
from services.subtitles.domain.SubtitleTiming import elapsed_ms_since, log_timing


logger = logging.getLogger(__name__)


class SubtitleGenerationRuntimeCoordinator:
    def __init__(
        self,
        *,
        player,
        ui,
        pipeline_state: SubtitlePipelineStateMachine,
        transitions: SubtitlePipelineTransitions,
        shutdown: SubtitleShutdownCoordinator,
        audio_probe_flow,
        cuda_runtime_flow,
        whisper_model_flow,
        subtitle_job_runner: SubtitleGenerationJobRunner,
        assert_pipeline_thread: Callable[[], None],
        on_shutdown_finished: Callable[[], None],
    ):
        self._player = player
        self._ui = ui
        self._pipeline_state = pipeline_state
        self._transitions = transitions
        self._shutdown = shutdown
        self._audio_probe_flow = audio_probe_flow
        self._cuda_runtime_flow = cuda_runtime_flow
        self._whisper_model_flow = whisper_model_flow
        self._subtitle_job_runner = subtitle_job_runner
        self._assert_pipeline_thread = assert_pipeline_thread
        self._on_shutdown_finished = on_shutdown_finished
        self._pending_subtitle_thread_run_ids: set[int] = set()
        self._playback_takeover = self._player.playback.create_interruption_lease("subtitle_generation")
        self._player_ui_suspend_lease = None

    @property
    def pending_subtitle_thread_run_ids(self) -> set[int]:
        return self._pending_subtitle_thread_run_ids

    @property
    def playback_takeover(self):
        return self._playback_takeover

    @property
    def player_ui_suspend_lease(self):
        return self._player_ui_suspend_lease

    def launch_subtitle_generation(self, run: SubtitlePipelineRun, options) -> None:
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

        self._transitions.start_subtitle_generation(
            run,
            close_dialog=not self._pipeline_state.is_shutdown_in_progress(),
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
        self._ui.open_generation_progress(options, on_cancel=self.request_active_task_stop)

        self._pending_subtitle_thread_run_ids.add(run.run_id)
        self._subtitle_job_runner.start(run, options)

    def on_subtitle_worker_start_aborted(self, run_id: int, thread, worker) -> None:
        self._assert_pipeline_thread()
        self._pending_subtitle_thread_run_ids.discard(run_id)
        run = self._pipeline_state.active_job
        if run is not None and run.run_id == run_id and run.subtitle_thread is thread and run.subtitle_worker is worker:
            self._clear_subtitle_runtime(run)
            self._release_player_ui_suspend_lease()
        self.complete_shutdown_if_possible()

    def can_start_subtitle_worker(self, run_id: int, thread, worker) -> bool:
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

    def request_active_task_stop(self, force: bool = False) -> None:
        self._assert_pipeline_thread()
        run = self._pipeline_state.active_job
        if run is None or not run.accepts_stop_requests():
            logger.debug(
                "Active task stop ignored because pipeline is not stoppable | run_phase=%s | force=%s",
                run.phase.name if run is not None else "<none>",
                force,
            )
            return

        if run.task in (SubtitlePipelineTask.NONE, SubtitlePipelineTask.CUDA_PROMPT, SubtitlePipelineTask.MODEL_PROMPT):
            logger.debug("Active task stop ignored because no pipeline task is active | force=%s", force)
            return

        task_control = self._task_control_for_run(run)
        if task_control is None:
            logger.debug("Active task stop ignored because no task control exists | task=%s | force=%s", run.task.name, force)
            return

        self._transitions.mark_run_canceling(run)
        if not task_control.request_stop(force=force):
            return

        if not force:
            self._show_active_task_cancel_pending(run.task)

    def has_active_tasks(self) -> bool:
        return self._shutdown.has_active_tasks(
            background_task_active=self._background_task_is_active(),
            audio_probe_active=self._audio_probe_flow.is_active(),
        )

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
        self._apply_shutdown_action(action, invalidate_reason="shutdown", stop_audio_probe_reason=None)
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
        self._apply_shutdown_action(action, invalidate_reason=None, stop_audio_probe_reason="shutdown")
        return self.has_active_tasks()

    def begin_emergency_shutdown(self) -> bool:
        self._assert_pipeline_thread()
        logger.critical("Subtitle generation service emergency shutdown escalation requested")

        if self._shutdown.completed:
            logger.debug("Subtitle generation service emergency shutdown requested after completion")
            return False

        if not self._shutdown.is_shutdown_in_progress():
            return self.begin_force_shutdown()

        self._ui.close_generation_dialog()
        self._ui.close_progress_dialog()
        self.request_active_task_stop(force=True)
        self._audio_probe_flow.stop_all("emergency-shutdown", force=True)
        self.complete_shutdown_if_possible()
        return self.has_active_tasks()

    def on_background_task_thread_finished(self, run_id: int, task: SubtitlePipelineTask) -> None:
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
            self._transitions.discard_active_job()
        self.complete_shutdown_if_possible()

    def complete_run(self, run_id: int, terminal_phase: SubtitlePipelinePhase, *, close_progress: bool) -> None:
        self._assert_pipeline_thread()
        run = self._require_active_job(run_id, "terminal transition")
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
        self._transitions.complete_run(
            run,
            terminal_phase,
            clear_active_job=clear_active_job,
            record_result=not is_shutdown,
        )
        self._transitions.settle_after_terminal_run(run_id=run_id, is_shutdown=is_shutdown)
        self.release_playback_takeover(resume_playback=not is_shutdown)
        self.complete_shutdown_if_possible()

    def suspend_player_ui_for_generation(self) -> None:
        if self._player_ui_suspend_lease is not None:
            return
        self._player_ui_suspend_lease = self._player.suspend_for_subtitle_generation()

    def release_playback_takeover(self, *, resume_playback: bool) -> None:
        self._release_player_ui_suspend_lease()
        self._playback_takeover.release(resume_playback=resume_playback)

    def _release_player_ui_suspend_lease(self) -> None:
        player_ui_suspend_lease = self._player_ui_suspend_lease
        self._player_ui_suspend_lease = None
        if player_ui_suspend_lease is not None:
            player_ui_suspend_lease.release()

    def complete_shutdown_if_possible(self) -> None:
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
        self._on_shutdown_finished()

    def _apply_shutdown_action(
        self,
        action: SubtitleShutdownAction,
        *,
        invalidate_reason: str | None,
        stop_audio_probe_reason: str | None,
    ) -> None:
        if action.close_generation_dialog:
            self._ui.close_generation_dialog()
        if action.close_progress_dialog:
            self._ui.close_progress_dialog()
        if action.request_task_stop:
            self.request_active_task_stop(force=action.force_task_stop)
        if action.invalidate_audio_probe and invalidate_reason is not None:
            self._audio_probe_flow.invalidate_active_request(invalidate_reason)
        if action.stop_audio_probe and stop_audio_probe_reason is not None:
            self._audio_probe_flow.stop_all(stop_audio_probe_reason, force=True)
        self.complete_shutdown_if_possible()

    def _subtitle_worker_task_control(self, run: SubtitlePipelineRun) -> SubtitleTaskControl:
        return SubtitleWorkerTaskControl(run, self._pending_subtitle_thread_run_ids)

    def _cuda_runtime_task_control(self, run: SubtitlePipelineRun) -> SubtitleTaskControl:
        return CudaRuntimeTaskControl(run, self._cuda_runtime_flow)

    def _whisper_model_task_control(self, run: SubtitlePipelineRun) -> SubtitleTaskControl:
        return WhisperModelTaskControl(run, self._whisper_model_flow)

    def _task_control_for_run(self, run: SubtitlePipelineRun) -> SubtitleTaskControl | None:
        if run.task == SubtitlePipelineTask.SUBTITLE_GENERATION:
            return self._subtitle_worker_task_control(run)
        if run.task == SubtitlePipelineTask.CUDA_INSTALL:
            return self._cuda_runtime_task_control(run)
        if run.task == SubtitlePipelineTask.MODEL_INSTALL:
            return self._whisper_model_task_control(run)
        return None

    def _background_task_is_active(self) -> bool:
        run = self._pipeline_state.active_job
        if run is not None and run.keeps_shutdown_pending():
            return True

        task_control = self._task_control_for_run(run) if run is not None else None
        return task_control is not None and task_control.is_active()

    def _show_active_task_cancel_pending(self, task: SubtitlePipelineTask) -> None:
        if task == SubtitlePipelineTask.SUBTITLE_GENERATION:
            self._ui.show_subtitle_cancel_pending()
        elif task == SubtitlePipelineTask.CUDA_INSTALL:
            self._ui.show_cuda_install_cancel_pending()
        elif task == SubtitlePipelineTask.MODEL_INSTALL:
            self._ui.show_model_install_cancel_pending()

    def _clear_subtitle_runtime(self, run: SubtitlePipelineRun) -> None:
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

    def _require_active_job(self, run_id: int, event_name: str) -> SubtitlePipelineRun | None:
        run = self._pipeline_state.active_job
        if run is None:
            logger.debug("Ignoring %s for stale subtitle pipeline run | run_id=%s | active_job=<none>", event_name, run_id)
            return None
        if run.run_id != run_id:
            logger.debug(
                "Ignoring %s for stale subtitle pipeline run | run_id=%s | active_job=%s",
                event_name,
                run_id,
                run.run_id,
            )
            return None
        return run

    def _finalize_shutdown_lifecycle(self) -> None:
        self._assert_pipeline_thread()
        self._ui.close_progress_dialog()
        self._transitions.discard_active_job()
        self._audio_probe_flow.stop_all("finalize-shutdown", force=True)
        self.release_playback_takeover(resume_playback=False)
        self._shutdown.mark_finished()
