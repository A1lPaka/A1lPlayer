from __future__ import annotations

import logging
import os
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

from models import SubtitleGenerationDialogResult
from services.subtitles.CudaRuntimeDiscovery import get_missing_windows_cuda_runtime_packages
from services.subtitles.SubtitleGenerationOutcomePresenter import SubtitleGenerationOutcomePresenter
from services.subtitles.SubtitleGenerationPreflight import (
    SubtitleGenerationPreflight,
    SubtitleGenerationValidationFailure,
    SubtitleGenerationValidationResult,
)
from services.subtitles.SubtitlePipelineState import (
    SubtitleGenerationContext,
    SubtitlePipelinePhase,
    SubtitlePipelineRun,
    SubtitlePipelineStateMachine,
)
from services.subtitles.SubtitlePipelineTransitions import SubtitlePipelineTransitions
from services.subtitles.SubtitleTiming import elapsed_ms_since, log_timing
from ui.MessageBoxService import prompt_cuda_runtime_choice


logger = logging.getLogger(__name__)


class SubtitleGenerationStartFlow:
    def __init__(
        self,
        *,
        parent,
        player,
        ui,
        preflight: SubtitleGenerationPreflight,
        validation_presenter,
        audio_probe_flow,
        pipeline_state: SubtitlePipelineStateMachine,
        transitions: SubtitlePipelineTransitions,
        cuda_runtime_flow,
        outcome_presenter: SubtitleGenerationOutcomePresenter,
        assert_pipeline_thread: Callable[[], None],
        log_dialog_confirm_timing: Callable[[str], None],
        launch_subtitle_generation: Callable[[SubtitlePipelineRun, SubtitleGenerationDialogResult], None],
        complete_run: Callable[[int, SubtitlePipelinePhase, bool], None],
        request_active_task_stop: Callable[[], None],
    ):
        self._parent = parent
        self._player = player
        self._ui = ui
        self._preflight = preflight
        self._validation_presenter = validation_presenter
        self._audio_probe_flow = audio_probe_flow
        self._pipeline_state = pipeline_state
        self._transitions = transitions
        self._cuda_runtime_flow = cuda_runtime_flow
        self._outcome_presenter = outcome_presenter
        self._assert_pipeline_thread = assert_pipeline_thread
        self._log_dialog_confirm_timing = log_dialog_confirm_timing
        self._launch_subtitle_generation = launch_subtitle_generation
        self._complete_run = complete_run
        self._request_active_task_stop = request_active_task_stop

    def start(self, options: SubtitleGenerationDialogResult):
        self._assert_pipeline_thread()
        self._log_dialog_confirm_timing(options.output_path)
        if not self._pipeline_state.can_accept_generation_start():
            logger.warning(
                "Rejected subtitle generation start because the generation dialog is not active | state=%s",
                self._pipeline_state.dialog_lifecycle_state.name,
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

        run = self._transitions.begin_run(generation_context, options)
        preflight_started_at = time.perf_counter()
        validation_result = self._preflight.validate_generation_request(
            current_media_path,
            options,
            probe_state=self._audio_probe_flow.probe_state_for_media(current_media_path),
            audio_streams=self._audio_probe_flow.get_cached_audio_streams_for_media(current_media_path),
            probe_error=self._audio_probe_flow.get_cached_audio_stream_error_for_media(current_media_path),
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
        if not self._validation_presenter.confirm_or_show_failure(validation_result):
            self._discard_starting_run("subtitle generation preflight failed")
            return
        options = self._apply_overwrite_confirmation(options, validation_result)
        run.requested_options = options

        resolved_options = self._resolve_cuda_runtime_options(options, run)
        if resolved_options is None:
            if self._pipeline_state.active_job is run and run.phase == SubtitlePipelinePhase.STARTING:
                self._discard_starting_run("subtitle launch postponed or canceled during CUDA resolution")
            return

        latest_context = self._capture_current_generation_context()
        if latest_context is None:
            logger.warning("Subtitle generation aborted because playback context is unavailable before launch")
            self._transitions.discard_active_job()
            self._transition_back_to_dialog("playback context unavailable before launch")
            return
        if latest_context != run.context:
            logger.warning(
                "Subtitle generation aborted because playback context changed before launch | run_id=%s | original_media=%s | original_request_id=%s | active_media=%s | active_request_id=%s",
                run.run_id,
                run.context.media_path,
                run.context.request_id,
                latest_context.media_path,
                latest_context.request_id,
            )
            self._transitions.discard_active_job()
            self._transition_back_to_dialog("playback context changed before launch")
            return

        run.subtitle_options = resolved_options
        self._launch_subtitle_generation(run, resolved_options)

    def start_cuda_runtime_install(
        self,
        run: SubtitlePipelineRun,
        missing_packages: list[str],
    ):
        self._assert_pipeline_thread()
        if run is not self._pipeline_state.active_job:
            logger.debug("Ignoring CUDA runtime install start for stale run | run_id=%s", run.run_id)
            return
        if run.phase != SubtitlePipelinePhase.STARTING:
            logger.warning(
                "Rejected CUDA runtime install start because run phase is not launchable | run_id=%s | phase=%s",
                run.run_id,
                run.phase.name,
            )
            return

        self._transitions.start_cuda_runtime_install(
            run,
            close_dialog=not self._pipeline_state.is_shutdown_in_progress(),
        )

        logger.info(
            "Starting CUDA runtime install flow | run_id=%s | media=%s | request_id=%s | packages=%s",
            run.run_id,
            run.context.media_path,
            run.context.request_id,
            ", ".join(missing_packages),
        )
        self._ui.open_cuda_install_progress(
            missing_packages,
            on_cancel=self._request_active_task_stop,
        )
        if not self._cuda_runtime_flow.start(run.run_id, missing_packages):
            logger.error("CUDA runtime install flow could not be started | run_id=%s", run.run_id)
            self._complete_run(run.run_id, SubtitlePipelinePhase.FAILED, True)
            if not self._pipeline_state.is_shutdown_in_progress():
                self._outcome_presenter.show_cuda_runtime_install_failed(
                    "GPU runtime installation could not be started."
                )
            return

    def _resolve_cuda_runtime_options(
        self,
        options: SubtitleGenerationDialogResult,
        run: SubtitlePipelineRun,
    ) -> SubtitleGenerationDialogResult | None:
        self._assert_pipeline_thread()
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
        self._transitions.enter_cuda_runtime_prompt(run)
        try:
            choice = prompt_cuda_runtime_choice(self._parent, missing_packages)
        finally:
            self._transitions.leave_cuda_runtime_prompt(run)
        if not self._transitions.should_present_terminal_feedback():
            logger.info("CUDA runtime prompt returned after shutdown started | run_id=%s", run.run_id)
            self._complete_run(run.run_id, SubtitlePipelinePhase.CANCELED, True)
            return None
        if not self._starting_run_matches_current_context(run, "CUDA runtime prompt"):
            return None

        if choice == "cancel":
            logger.info("User canceled subtitle generation after CUDA runtime prompt | run_id=%s", run.run_id)
            return None

        if choice == "cpu":
            logger.info("User switched subtitle generation from CUDA to CPU | run_id=%s", run.run_id)
            return replace(options, device="cpu")

        run.subtitle_options = options
        self.start_cuda_runtime_install(run, missing_packages)
        return None

    def _starting_run_matches_current_context(self, run: SubtitlePipelineRun, event_name: str) -> bool:
        self._assert_pipeline_thread()
        if run is not self._pipeline_state.active_job:
            logger.debug("Ignoring %s result for stale subtitle pipeline run | run_id=%s", event_name, run.run_id)
            return False
        if run.phase != SubtitlePipelinePhase.STARTING:
            logger.warning(
                "Rejected %s result because run phase is not launchable | run_id=%s | phase=%s",
                event_name,
                run.run_id,
                run.phase.name,
            )
            return False
        latest_context = self._capture_current_generation_context()
        if latest_context == run.context:
            return True

        logger.warning(
            "Rejected %s result because playback context changed | run_id=%s | original_media=%s | original_request_id=%s | active_media=%s | active_request_id=%s",
            event_name,
            run.run_id,
            run.context.media_path,
            run.context.request_id,
            latest_context.media_path if latest_context is not None else "<none>",
            latest_context.request_id if latest_context is not None else "<none>",
        )
        return False

    def _capture_current_generation_context(self) -> SubtitleGenerationContext | None:
        media_path = self._player.playback.current_media_path()
        if not media_path:
            return None

        return SubtitleGenerationContext(
            media_path=media_path,
            request_id=self._player.playback.current_request_id(),
        )

    def _apply_overwrite_confirmation(
        self,
        options: SubtitleGenerationDialogResult,
        validation_result: SubtitleGenerationValidationResult,
    ) -> SubtitleGenerationDialogResult:
        if validation_result.reason != SubtitleGenerationValidationFailure.OVERWRITE_CONFIRMATION_REQUIRED:
            return options

        output_path = validation_result.output_path or options.output_path
        return replace(
            options,
            overwrite_confirmed_for_path=self._normalize_output_path_for_confirmation(output_path),
        )

    def _normalize_output_path_for_confirmation(self, output_path: str) -> str:
        try:
            return os.path.normcase(str(Path(output_path).expanduser().resolve(strict=False)))
        except (OSError, RuntimeError, ValueError):
            return str(output_path)

    def _discard_starting_run(self, reason: str):
        self._assert_pipeline_thread()
        run = self._pipeline_state.active_job
        if run is not None:
            logger.debug("Discarding starting subtitle pipeline run | run_id=%s | reason=%s", run.run_id, reason)
            self._clear_subtitle_runtime(run)
        self._transitions.revert_start_to_dialog(reason)

    def _clear_subtitle_runtime(self, run: SubtitlePipelineRun):
        run.subtitle_thread = None
        run.subtitle_worker = None
        run.subtitle_cancel_requested = False

    def _transition_back_to_dialog(self, reason: str):
        self._assert_pipeline_thread()
        self._transitions.keep_generation_dialog_open(reason)
