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


def test_state_machine_rejects_disallowed_service_transition():
    state = SubtitlePipelineStateMachine()

    changed = state.transition_service_state(
        SubtitleServiceState.SHUTTING_DOWN,
        "invalid test transition",
        allowed=(SubtitleServiceState.DIALOG_OPEN,),
    )

    assert changed is False
    assert state.service_state == SubtitleServiceState.IDLE


def test_state_machine_begins_and_completes_run():
    state = SubtitlePipelineStateMachine()
    context = SubtitleGenerationContext(media_path="C:/media/movie.mkv", request_id=7)

    run = state.begin_run(context, _options())
    state.set_run_phase(run, SubtitlePipelinePhase.RUNNING, "test launch")
    state.complete_run(
        run,
        SubtitlePipelinePhase.SUCCEEDED,
        clear_active_run=True,
        record_result=True,
    )

    assert run.run_id == 1
    assert run.phase == SubtitlePipelinePhase.SUCCEEDED
    assert state.active_run is None
    assert state.last_result == SubtitlePipelineResult.SUCCEEDED
