from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtCore import QThread

    from models.SubtitleGenerationDialogResult import SubtitleGenerationDialogResult
    from services.subtitles.workers.SubtitleGenerationWorkers import SubtitleGenerationWorker


logger = logging.getLogger(__name__)


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


class SubtitlePipelineTask(Enum):
    NONE = auto()
    CUDA_PROMPT = auto()
    CUDA_INSTALL = auto()
    MODEL_PROMPT = auto()
    MODEL_INSTALL = auto()
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
        if self.task in (SubtitlePipelineTask.CUDA_PROMPT, SubtitlePipelineTask.MODEL_PROMPT):
            return self.phase == SubtitlePipelinePhase.STARTING

        return self.task != SubtitlePipelineTask.NONE and self.phase in (
            SubtitlePipelinePhase.RUNNING,
            SubtitlePipelinePhase.CANCELING,
        )


class SubtitlePipelineStateMachine:
    def __init__(self):
        self.dialog_lifecycle_state = SubtitleServiceState.IDLE
        self.active_job: SubtitlePipelineRun | None = None
        self.last_result: SubtitlePipelineResult | None = None
        self._next_run_id = 1

    @property
    def active_job_lifecycle_state(self) -> SubtitlePipelinePhase | None:
        if self.active_job is None:
            return None
        return self.active_job.phase

    def can_open_generation_dialog(self) -> bool:
        return self.dialog_lifecycle_state == SubtitleServiceState.IDLE and not self.blocks_new_generation_request()

    def has_dialog_open(self) -> bool:
        return self.dialog_lifecycle_state == SubtitleServiceState.DIALOG_OPEN

    def blocks_new_generation_request(self) -> bool:
        return self.active_job is not None and self.active_job.blocks_new_requests()

    def is_shutdown_in_progress(self) -> bool:
        return self.dialog_lifecycle_state == SubtitleServiceState.SHUTTING_DOWN

    def can_accept_generation_start(self) -> bool:
        return self.has_dialog_open()

    def transition_dialog_lifecycle_state(
        self,
        new_dialog_lifecycle_state: SubtitleServiceState,
        reason: str,
        *,
        allowed: tuple[SubtitleServiceState, ...],
    ) -> bool:
        if self.dialog_lifecycle_state not in allowed:
            logger.warning(
                "Rejected subtitle dialog lifecycle transition | from=%s | to=%s | reason=%s",
                self.dialog_lifecycle_state.name,
                new_dialog_lifecycle_state.name,
                reason,
            )
            return False

        if self.dialog_lifecycle_state != new_dialog_lifecycle_state:
            logger.debug(
                "Subtitle dialog lifecycle transition | from=%s | to=%s | reason=%s",
                self.dialog_lifecycle_state.name,
                new_dialog_lifecycle_state.name,
                reason,
            )
        self.dialog_lifecycle_state = new_dialog_lifecycle_state
        return True

    def begin_run(
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
        self.active_job = run
        return run

    def set_run_phase(self, run: SubtitlePipelineRun, phase: SubtitlePipelinePhase, reason: str):
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

    def discard_active_job(self):
        self.active_job = None

    def complete_run(
        self,
        run: SubtitlePipelineRun,
        terminal_phase: SubtitlePipelinePhase,
        *,
        clear_active_job: bool,
        record_result: bool,
    ):
        if terminal_phase not in (
            SubtitlePipelinePhase.SUCCEEDED,
            SubtitlePipelinePhase.FAILED,
            SubtitlePipelinePhase.CANCELED,
        ):
            raise ValueError(f"Unsupported terminal state: {terminal_phase}")

        self.set_run_phase(run, terminal_phase, f"complete run {run.run_id}")
        if clear_active_job and self.active_job is run:
            self.active_job = None
        if record_result:
            self.last_result = self._result_from_terminal_phase(terminal_phase)

    @staticmethod
    def _result_from_terminal_phase(terminal_phase: SubtitlePipelinePhase) -> SubtitlePipelineResult:
        if terminal_phase == SubtitlePipelinePhase.SUCCEEDED:
            return SubtitlePipelineResult.SUCCEEDED
        if terminal_phase == SubtitlePipelinePhase.FAILED:
            return SubtitlePipelineResult.FAILED
        if terminal_phase == SubtitlePipelinePhase.CANCELED:
            return SubtitlePipelineResult.CANCELED
        raise ValueError(f"Unsupported terminal phase: {terminal_phase}")
