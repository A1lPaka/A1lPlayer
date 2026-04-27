from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from models import SubtitleGenerationDialogResult
from services.subtitles.SubtitlePipelineState import (
    SubtitleGenerationContext,
    SubtitlePipelinePhase,
    SubtitlePipelineRun,
    SubtitlePipelineStateMachine,
    SubtitlePipelineTask,
    SubtitleServiceState,
)


class CudaInstallCompletionDecision(Enum):
    RELAUNCH_SUBTITLE_GENERATION = auto()
    COMPLETE_AS_CANCELED = auto()
    FAIL_MISSING_OPTIONS = auto()


@dataclass(frozen=True)
class CudaInstallCompletionPlan:
    decision: CudaInstallCompletionDecision
    run: SubtitlePipelineRun


class SubtitlePipelineTransitions:
    def __init__(self, pipeline_state: SubtitlePipelineStateMachine):
        self._pipeline_state = pipeline_state

    def open_generation_dialog(self) -> bool:
        return self._pipeline_state.transition_dialog_lifecycle_state(
            SubtitleServiceState.DIALOG_OPEN,
            "open generation dialog",
            allowed=(SubtitleServiceState.IDLE,),
        )

    def close_generation_dialog(self, reason: str) -> bool:
        return self._pipeline_state.transition_dialog_lifecycle_state(
            SubtitleServiceState.IDLE,
            reason,
            allowed=(SubtitleServiceState.DIALOG_OPEN,),
        )

    def keep_generation_dialog_open(self, reason: str) -> bool:
        return self._pipeline_state.transition_dialog_lifecycle_state(
            SubtitleServiceState.DIALOG_OPEN,
            reason,
            allowed=(SubtitleServiceState.DIALOG_OPEN,),
        )

    def close_dialog_for_background_task(self, reason: str) -> bool:
        return self._pipeline_state.transition_dialog_lifecycle_state(
            SubtitleServiceState.IDLE,
            reason,
            allowed=(SubtitleServiceState.DIALOG_OPEN, SubtitleServiceState.IDLE),
        )

    def begin_run(
        self,
        generation_context: SubtitleGenerationContext,
        options: SubtitleGenerationDialogResult,
    ) -> SubtitlePipelineRun:
        return self._pipeline_state.begin_run(generation_context, options)

    def active_run(self) -> SubtitlePipelineRun | None:
        return self._pipeline_state.active_job

    def active_run_for_id(self, run_id: int) -> SubtitlePipelineRun | None:
        run = self._pipeline_state.active_job
        if run is not None and run.run_id == run_id:
            return run
        return None

    def discard_active_job(self) -> None:
        self._pipeline_state.discard_active_job()

    def revert_start_to_dialog(self, reason: str) -> None:
        self.discard_active_job()
        self.keep_generation_dialog_open(reason)

    def begin_shutdown(self, reason: str) -> bool:
        return self._pipeline_state.transition_dialog_lifecycle_state(
            SubtitleServiceState.SHUTTING_DOWN,
            reason,
            allowed=tuple(SubtitleServiceState),
        )

    def start_subtitle_generation(self, run: SubtitlePipelineRun, *, close_dialog: bool) -> None:
        self._pipeline_state.set_run_phase(run, SubtitlePipelinePhase.RUNNING, "launch subtitle generation worker")
        run.task = SubtitlePipelineTask.SUBTITLE_GENERATION
        if close_dialog:
            self.close_dialog_for_background_task("generation dialog replaced by subtitle progress")

    def start_cuda_runtime_install(self, run: SubtitlePipelineRun, *, close_dialog: bool) -> None:
        self._pipeline_state.set_run_phase(run, SubtitlePipelinePhase.RUNNING, "start CUDA runtime install")
        run.task = SubtitlePipelineTask.CUDA_INSTALL
        if close_dialog:
            self.close_dialog_for_background_task("generation dialog replaced by CUDA runtime progress")

    def enter_cuda_runtime_prompt(self, run: SubtitlePipelineRun) -> None:
        run.task = SubtitlePipelineTask.CUDA_PROMPT

    def leave_cuda_runtime_prompt(self, run: SubtitlePipelineRun) -> None:
        if self.active_run_for_id(run.run_id) is run and run.task == SubtitlePipelineTask.CUDA_PROMPT:
            run.task = SubtitlePipelineTask.NONE

    def mark_run_canceling(self, run: SubtitlePipelineRun) -> None:
        self._pipeline_state.set_run_phase(
            run,
            SubtitlePipelinePhase.CANCELING,
            f"request stop for {run.task.name.lower()}",
        )

    def complete_run(
        self,
        run: SubtitlePipelineRun,
        terminal_phase: SubtitlePipelinePhase,
        *,
        clear_active_job: bool,
        record_result: bool,
    ) -> None:
        self._pipeline_state.complete_run(
            run,
            terminal_phase,
            clear_active_job=clear_active_job,
            record_result=record_result,
        )

    def settle_after_terminal_run(self, *, run_id: int, is_shutdown: bool) -> None:
        if is_shutdown:
            return
        self._pipeline_state.transition_dialog_lifecycle_state(
            SubtitleServiceState.IDLE,
            f"record terminal result for run {run_id}",
            allowed=(SubtitleServiceState.IDLE, SubtitleServiceState.DIALOG_OPEN),
        )

    def should_present_terminal_feedback(self) -> bool:
        return not self._pipeline_state.is_shutdown_in_progress()

    def plan_cuda_install_completion(self, run_id: int) -> CudaInstallCompletionPlan | None:
        run = self.active_run_for_id(run_id)
        if run is None:
            return None
        if run.phase == SubtitlePipelinePhase.CANCELING or self._pipeline_state.is_shutdown_in_progress():
            return CudaInstallCompletionPlan(CudaInstallCompletionDecision.COMPLETE_AS_CANCELED, run)
        if run.subtitle_options is None:
            return CudaInstallCompletionPlan(CudaInstallCompletionDecision.FAIL_MISSING_OPTIONS, run)
        return CudaInstallCompletionPlan(CudaInstallCompletionDecision.RELAUNCH_SUBTITLE_GENERATION, run)
