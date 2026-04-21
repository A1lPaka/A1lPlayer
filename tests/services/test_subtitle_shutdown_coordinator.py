from models import SubtitleGenerationDialogResult
from services.subtitles.SubtitlePipelineState import (
    SubtitleGenerationContext,
    SubtitlePipelinePhase,
    SubtitlePipelineStateMachine,
    SubtitlePipelineTask,
    SubtitleServiceState,
)
from services.subtitles.SubtitleShutdownCoordinator import (
    SubtitleShutdownCoordinator,
    SubtitleShutdownDecision,
)


def _options() -> SubtitleGenerationDialogResult:
    return SubtitleGenerationDialogResult(
        audio_stream_index=None,
        audio_language=None,
        device=None,
        model_size="small",
        output_format="srt",
        output_path="C:/media/movie.srt",
        auto_open_after_generation=True,
    )


def _running_job(state: SubtitlePipelineStateMachine):
    run = state.begin_run(
        SubtitleGenerationContext(media_path="C:/media/movie.mkv", request_id=7),
        _options(),
    )
    state.set_run_phase(run, SubtitlePipelinePhase.RUNNING, "test run")
    run.task = SubtitlePipelineTask.SUBTITLE_GENERATION
    return run


def test_graceful_shutdown_starts_once_and_repeats_without_actions():
    state = SubtitlePipelineStateMachine()
    shutdown = SubtitleShutdownCoordinator(state)

    action = shutdown.begin_graceful_shutdown()
    repeated = shutdown.begin_graceful_shutdown()

    assert action.decision == SubtitleShutdownDecision.START_GRACEFUL
    assert action.close_generation_dialog is True
    assert action.request_task_stop is True
    assert action.invalidate_audio_probe is True
    assert state.dialog_lifecycle_state == SubtitleServiceState.SHUTTING_DOWN
    assert repeated.decision == SubtitleShutdownDecision.REPEATED_GRACEFUL
    assert repeated.request_task_stop is False


def test_force_shutdown_escalates_once():
    state = SubtitlePipelineStateMachine()
    shutdown = SubtitleShutdownCoordinator(state)

    action = shutdown.begin_force_shutdown()
    repeated = shutdown.begin_force_shutdown()

    assert action.decision == SubtitleShutdownDecision.START_FORCE
    assert action.close_generation_dialog is True
    assert action.close_progress_dialog is True
    assert action.request_task_stop is True
    assert action.force_task_stop is True
    assert action.stop_audio_probe is True
    assert shutdown.force_requested is True
    assert repeated.decision == SubtitleShutdownDecision.REPEATED_FORCE
    assert repeated.force_task_stop is False


def test_active_tasks_include_threads_flows_and_running_job():
    state = SubtitlePipelineStateMachine()
    shutdown = SubtitleShutdownCoordinator(state)

    assert shutdown.has_active_tasks(
        has_pending_subtitle_thread=True,
        cuda_runtime_active=False,
        audio_probe_active=False,
    )
    assert shutdown.has_active_tasks(
        has_pending_subtitle_thread=False,
        cuda_runtime_active=True,
        audio_probe_active=False,
    )
    assert shutdown.has_active_tasks(
        has_pending_subtitle_thread=False,
        cuda_runtime_active=False,
        audio_probe_active=True,
    )

    _running_job(state)

    assert shutdown.has_active_tasks(
        has_pending_subtitle_thread=False,
        cuda_runtime_active=False,
        audio_probe_active=False,
    )


def test_shutdown_finished_is_emitted_only_after_shutdown_has_no_active_tasks():
    state = SubtitlePipelineStateMachine()
    shutdown = SubtitleShutdownCoordinator(state)

    assert shutdown.should_emit_shutdown_finished(
        has_pending_subtitle_thread=False,
        cuda_runtime_active=False,
        audio_probe_active=False,
    ) is False

    shutdown.begin_graceful_shutdown()

    assert shutdown.should_emit_shutdown_finished(
        has_pending_subtitle_thread=True,
        cuda_runtime_active=False,
        audio_probe_active=False,
    ) is False
    assert shutdown.should_emit_shutdown_finished(
        has_pending_subtitle_thread=False,
        cuda_runtime_active=False,
        audio_probe_active=False,
    ) is True

    shutdown.mark_finished()

    assert shutdown.should_emit_shutdown_finished(
        has_pending_subtitle_thread=False,
        cuda_runtime_active=False,
        audio_probe_active=False,
    ) is False
