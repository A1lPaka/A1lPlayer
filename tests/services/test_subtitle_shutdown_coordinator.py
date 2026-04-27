from services.subtitles.state.SubtitlePipelineState import (
    SubtitlePipelineStateMachine,
    SubtitleServiceState,
)
from services.subtitles.state.SubtitlePipelineTransitions import SubtitlePipelineTransitions
from services.subtitles.state.SubtitleShutdownCoordinator import (
    SubtitleShutdownCoordinator,
    SubtitleShutdownDecision,
)


def test_graceful_shutdown_starts_once_and_repeats_without_actions():
    state = SubtitlePipelineStateMachine()
    shutdown = SubtitleShutdownCoordinator(state, SubtitlePipelineTransitions(state))

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
    shutdown = SubtitleShutdownCoordinator(state, SubtitlePipelineTransitions(state))

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


def test_active_tasks_include_background_task_or_audio_probe():
    state = SubtitlePipelineStateMachine()
    shutdown = SubtitleShutdownCoordinator(state, SubtitlePipelineTransitions(state))

    assert shutdown.has_active_tasks(
        background_task_active=True,
        audio_probe_active=False,
    )
    assert shutdown.has_active_tasks(
        background_task_active=False,
        audio_probe_active=True,
    )

    assert not shutdown.has_active_tasks(
        background_task_active=False,
        audio_probe_active=False,
    )


def test_shutdown_finished_is_emitted_only_after_shutdown_has_no_active_tasks():
    state = SubtitlePipelineStateMachine()
    shutdown = SubtitleShutdownCoordinator(state, SubtitlePipelineTransitions(state))

    assert shutdown.should_emit_shutdown_finished(
        background_task_active=False,
        audio_probe_active=False,
    ) is False

    shutdown.begin_graceful_shutdown()

    assert shutdown.should_emit_shutdown_finished(
        background_task_active=True,
        audio_probe_active=False,
    ) is False
    assert shutdown.should_emit_shutdown_finished(
        background_task_active=False,
        audio_probe_active=False,
    ) is True

    shutdown.mark_finished()

    assert shutdown.should_emit_shutdown_finished(
        background_task_active=False,
        audio_probe_active=False,
    ) is False
