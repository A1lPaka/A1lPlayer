from dataclasses import dataclass
from enum import Enum, auto

from services.subtitles.SubtitlePipelineState import SubtitlePipelineStateMachine, SubtitleServiceState


class SubtitleShutdownDecision(Enum):
    ALREADY_FINISHED = auto()
    REPEATED_GRACEFUL = auto()
    START_GRACEFUL = auto()
    REPEATED_FORCE = auto()
    START_FORCE = auto()


@dataclass(frozen=True)
class SubtitleShutdownAction:
    decision: SubtitleShutdownDecision
    close_generation_dialog: bool = False
    close_progress_dialog: bool = False
    request_task_stop: bool = False
    force_task_stop: bool = False
    invalidate_audio_probe: bool = False
    stop_audio_probe: bool = False


class SubtitleShutdownCoordinator:
    def __init__(self, pipeline_state: SubtitlePipelineStateMachine):
        self._pipeline_state = pipeline_state
        self.completed = False
        self.force_requested = False

    def has_active_tasks(
        self,
        *,
        has_pending_subtitle_thread: bool,
        cuda_runtime_active: bool,
        audio_probe_active: bool,
    ) -> bool:
        if has_pending_subtitle_thread or cuda_runtime_active or audio_probe_active:
            return True

        active_job = self._pipeline_state.active_job
        if active_job is None:
            return False

        return active_job.keeps_shutdown_pending()

    def is_shutdown_in_progress(self) -> bool:
        return self._pipeline_state.is_shutdown_in_progress() and not self.completed

    def begin_graceful_shutdown(self) -> SubtitleShutdownAction:
        if self.completed:
            return SubtitleShutdownAction(SubtitleShutdownDecision.ALREADY_FINISHED)

        if self._pipeline_state.is_shutdown_in_progress():
            return SubtitleShutdownAction(SubtitleShutdownDecision.REPEATED_GRACEFUL)

        self._pipeline_state.transition_dialog_lifecycle_state(
            SubtitleServiceState.SHUTTING_DOWN,
            "begin graceful shutdown",
            allowed=tuple(SubtitleServiceState),
        )
        self.force_requested = False
        return SubtitleShutdownAction(
            SubtitleShutdownDecision.START_GRACEFUL,
            close_generation_dialog=True,
            request_task_stop=True,
            invalidate_audio_probe=True,
        )

    def begin_force_shutdown(self) -> SubtitleShutdownAction:
        if self.completed:
            return SubtitleShutdownAction(SubtitleShutdownDecision.ALREADY_FINISHED)

        close_generation_dialog = False
        if not self._pipeline_state.is_shutdown_in_progress():
            self._pipeline_state.transition_dialog_lifecycle_state(
                SubtitleServiceState.SHUTTING_DOWN,
                "begin force shutdown",
                allowed=tuple(SubtitleServiceState),
            )
            close_generation_dialog = True

        if self.force_requested:
            return SubtitleShutdownAction(SubtitleShutdownDecision.REPEATED_FORCE)

        self.force_requested = True
        return SubtitleShutdownAction(
            SubtitleShutdownDecision.START_FORCE,
            close_generation_dialog=close_generation_dialog,
            close_progress_dialog=True,
            request_task_stop=True,
            force_task_stop=True,
            stop_audio_probe=True,
        )

    def should_emit_shutdown_finished(
        self,
        *,
        has_pending_subtitle_thread: bool,
        cuda_runtime_active: bool,
        audio_probe_active: bool,
    ) -> bool:
        return (
            self.is_shutdown_in_progress()
            and not self.has_active_tasks(
                has_pending_subtitle_thread=has_pending_subtitle_thread,
                cuda_runtime_active=cuda_runtime_active,
                audio_probe_active=audio_probe_active,
            )
        )

    def mark_finished(self):
        self.completed = True
        self.force_requested = False
