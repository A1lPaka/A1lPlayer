from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtCore import QThread

    from models import SubtitleGenerationDialogResult
    from services.subtitles.SubtitleGenerationWorkers import SubtitleGenerationWorker


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
