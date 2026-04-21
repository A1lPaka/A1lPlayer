from __future__ import annotations

import logging
from typing import Callable

from services.MediaLibraryService import SubtitleAttachResult
from services.MediaSettingsStore import MediaSettingsStore
from services.subtitles.SubtitleGenerationOutcomePresenter import (
    SubtitleAutoOpenOutcome,
    SubtitleGenerationOutcomePresenter,
)
from services.subtitles.SubtitlePipelineState import (
    SubtitlePipelinePhase,
    SubtitlePipelineRun,
    SubtitlePipelineStateMachine,
)


logger = logging.getLogger(__name__)


class SubtitleGenerationCompletionFlow:
    def __init__(
        self,
        *,
        store: MediaSettingsStore,
        media_library,
        ui,
        pipeline_state: SubtitlePipelineStateMachine,
        outcome_presenter: SubtitleGenerationOutcomePresenter,
        complete_run: Callable[[int, SubtitlePipelinePhase, bool], None],
        launch_subtitle_generation,
    ):
        self._store = store
        self._media_library = media_library
        self._ui = ui
        self._pipeline_state = pipeline_state
        self._outcome_presenter = outcome_presenter
        self._complete_run = complete_run
        self._launch_subtitle_generation = launch_subtitle_generation

    def handle_subtitle_generation_finished(
        self,
        run_id: int,
        output_path: str,
        auto_open: bool,
        used_fallback_output_path: bool,
    ):
        run = self._require_active_job(run_id, "subtitle generation finished")
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

        self._complete_run(run_id, SubtitlePipelinePhase.SUCCEEDED, True)

        if self._pipeline_state.is_shutdown_in_progress():
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
                logger.error(
                    "Generated subtitle could not be auto-loaded into playback | run_id=%s | output=%s",
                    run.run_id,
                    output_path,
                )
                auto_open_outcome = SubtitleAutoOpenOutcome.LOAD_FAILED
        else:
            self._store.save_last_open_dir(output_path)

        self._outcome_presenter.show_generation_success(
            output_path,
            auto_open_outcome,
            used_fallback_output_path=used_fallback_output_path,
            requested_output_path=(run.subtitle_options or run.requested_options).output_path,
        )

    def handle_subtitle_generation_failed(self, run_id: int, error_text: str, diagnostics: str):
        run = self._require_active_job(run_id, "subtitle generation failed")
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

        self._complete_run(run_id, SubtitlePipelinePhase.FAILED, True)

        if self._pipeline_state.is_shutdown_in_progress():
            return

        self._outcome_presenter.show_generation_failed(error_text)

    def handle_subtitle_generation_canceled(self, run_id: int):
        run = self._require_active_job(run_id, "subtitle generation canceled")
        if run is None:
            return

        logger.info("Subtitle generation canceled | run_id=%s", run.run_id)
        self._complete_run(run_id, SubtitlePipelinePhase.CANCELED, True)

        if self._pipeline_state.is_shutdown_in_progress():
            return

        self._outcome_presenter.show_generation_canceled()

    def handle_cuda_runtime_install_finished(self, run_id: int):
        run = self._require_active_job(run_id, "CUDA runtime install finished")
        if run is None:
            return

        logger.info("CUDA runtime install flow finished | run_id=%s", run.run_id)
        self._ui.close_progress_dialog()

        if run.phase == SubtitlePipelinePhase.CANCELING:
            logger.info(
                "Ignoring CUDA runtime completion because pipeline cancellation is already in progress | run_id=%s",
                run.run_id,
            )
            self._complete_run(run_id, SubtitlePipelinePhase.CANCELED, False)
            return

        if self._pipeline_state.is_shutdown_in_progress():
            self._complete_run(run_id, SubtitlePipelinePhase.CANCELED, False)
            return

        if run.subtitle_options is None:
            logger.warning("CUDA runtime install finished without pending subtitle generation options | run_id=%s", run.run_id)
            self._complete_run(run_id, SubtitlePipelinePhase.FAILED, False)
            self._outcome_presenter.show_cuda_runtime_install_failed(
                "GPU runtime installation finished without subtitle options.",
            )
            return

        self._launch_subtitle_generation(run, run.subtitle_options)

    def handle_cuda_runtime_install_failed(self, run_id: int, error_text: str):
        run = self._require_active_job(run_id, "CUDA runtime install failed")
        if run is None:
            return

        logger.error("CUDA runtime install failed | run_id=%s | message=%s", run.run_id, error_text)
        self._complete_run(run_id, SubtitlePipelinePhase.FAILED, True)

        if self._pipeline_state.is_shutdown_in_progress():
            return

        self._outcome_presenter.show_cuda_runtime_install_failed(error_text)

    def handle_cuda_runtime_install_canceled(self, run_id: int):
        run = self._require_active_job(run_id, "CUDA runtime install canceled")
        if run is None:
            return

        logger.info("CUDA runtime install canceled | run_id=%s", run.run_id)
        self._complete_run(run_id, SubtitlePipelinePhase.CANCELED, True)

        if self._pipeline_state.is_shutdown_in_progress():
            return

        self._outcome_presenter.show_cuda_runtime_install_canceled()

    def _require_active_job(self, run_id: int, event_name: str) -> SubtitlePipelineRun | None:
        run = self._pipeline_state.active_job
        if run is not None and run.run_id == run_id:
            return run
        logger.debug("Ignoring %s for stale subtitle pipeline run | run_id=%s", event_name, run_id)
        return None
