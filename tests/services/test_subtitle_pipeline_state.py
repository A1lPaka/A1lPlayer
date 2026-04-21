from models import SubtitleGenerationDialogResult
from services.subtitles.SubtitlePipelineState import (
    SubtitleGenerationContext,
    SubtitlePipelinePhase,
    SubtitlePipelineResult,
    SubtitlePipelineStateMachine,
    SubtitleServiceState,
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


def test_state_machine_rejects_disallowed_dialog_lifecycle_transition():
    state = SubtitlePipelineStateMachine()

    changed = state.transition_dialog_lifecycle_state(
        SubtitleServiceState.SHUTTING_DOWN,
        "invalid test transition",
        allowed=(SubtitleServiceState.DIALOG_OPEN,),
    )

    assert changed is False
    assert state.dialog_lifecycle_state == SubtitleServiceState.IDLE


def test_state_machine_exposes_dialog_and_job_lifecycle_queries():
    state = SubtitlePipelineStateMachine()
    context = SubtitleGenerationContext(media_path="C:/media/movie.mkv", request_id=7)

    assert state.can_open_generation_dialog() is True
    assert state.has_dialog_open() is False
    assert state.blocks_new_generation_request() is False
    assert state.is_shutdown_in_progress() is False
    assert state.can_accept_generation_start() is False

    state.transition_dialog_lifecycle_state(
        SubtitleServiceState.DIALOG_OPEN,
        "test open dialog",
        allowed=(SubtitleServiceState.IDLE,),
    )

    assert state.has_dialog_open() is True
    assert state.can_accept_generation_start() is True

    run = state.begin_run(context, _options())
    state.set_run_phase(run, SubtitlePipelinePhase.RUNNING, "test run")

    assert state.blocks_new_generation_request() is True
    assert state.active_job_lifecycle_state == SubtitlePipelinePhase.RUNNING
    assert state.can_open_generation_dialog() is False


def test_state_machine_begins_and_completes_run():
    state = SubtitlePipelineStateMachine()
    context = SubtitleGenerationContext(media_path="C:/media/movie.mkv", request_id=7)

    run = state.begin_run(context, _options())
    state.set_run_phase(run, SubtitlePipelinePhase.RUNNING, "test launch")
    state.complete_run(
        run,
        SubtitlePipelinePhase.SUCCEEDED,
        clear_active_job=True,
        record_result=True,
    )

    assert run.run_id == 1
    assert run.phase == SubtitlePipelinePhase.SUCCEEDED
    assert state.active_job is None
    assert state.last_result == SubtitlePipelineResult.SUCCEEDED
