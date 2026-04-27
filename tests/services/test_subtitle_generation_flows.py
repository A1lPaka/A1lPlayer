from PySide6.QtWidgets import QWidget

from models.SubtitleGenerationDialogResult import SubtitleGenerationDialogResult
from services.media.MediaLibraryService import MediaLibraryService
from services.subtitles.application.SubtitleGenerationCompletionFlow import SubtitleGenerationCompletionFlow
from services.subtitles.application.SubtitleGenerationStartFlow import SubtitleGenerationStartFlow
from services.subtitles.presentation.SubtitleGenerationOutcomePresenter import SubtitleAutoOpenOutcome
from services.subtitles.state.SubtitlePipelineState import (
    SubtitleGenerationContext,
    SubtitlePipelinePhase,
    SubtitlePipelineStateMachine,
)
from services.subtitles.state.SubtitlePipelineTransitions import SubtitlePipelineTransitions

from tests.fakes import FakeMediaStore, FakePlayerWindow


def _options(**overrides) -> SubtitleGenerationDialogResult:
    payload = {
        "audio_stream_index": None,
        "audio_language": None,
        "device": None,
        "model_size": "small",
        "output_format": "srt",
        "output_path": "C:/media/movie.srt",
        "auto_open_after_generation": True,
        "overwrite_confirmed_for_path": None,
    }
    payload.update(overrides)
    return SubtitleGenerationDialogResult(**payload)


class _PreflightStub:
    def __init__(self, result, on_validate=None):
        self._result = result
        self._on_validate = on_validate

    def validate_generation_request(self, *_args, **_kwargs):
        if self._on_validate is not None:
            self._on_validate()
        return self._result


class _ValidationPresenterStub:
    def __init__(self, accepted=True):
        self.accepted = accepted

    def confirm_or_show_failure(self, _result):
        return self.accepted


class _AudioProbeFlowStub:
    def __init__(self):
        self.probe_state = "ready"

    def probe_state_for_media(self, _media_path):
        return self.probe_state

    def get_cached_audio_streams_for_media(self, _media_path):
        return []

    def get_cached_audio_stream_error_for_media(self, _media_path):
        return None


class _UiStub:
    def __init__(self):
        self.cuda_progress_requests = []

    def open_cuda_install_progress(self, missing_packages, on_cancel):
        self.cuda_progress_requests.append((list(missing_packages), on_cancel))

    def close_progress_dialog(self):
        return None


class _ValidationResult:
    def __init__(self, *, is_valid=True, reason=None, output_path=None):
        self.is_valid = is_valid
        self.reason = reason
        self.output_path = output_path


class _OutcomePresenterStub:
    def __init__(self):
        self.success_calls = []

    def show_generation_success(self, output_path, auto_open_outcome, **kwargs):
        self.success_calls.append((output_path, auto_open_outcome, kwargs))


def _make_start_flow(player, *, preflight=None, validation_presenter=None):
    pipeline_state = SubtitlePipelineStateMachine()
    transitions = SubtitlePipelineTransitions(pipeline_state)
    assert transitions.open_generation_dialog() is True
    launch_calls = []
    complete_calls = []
    flow = SubtitleGenerationStartFlow(
        parent=QWidget(),
        player=player,
        ui=_UiStub(),
        preflight=preflight or _PreflightStub(_ValidationResult()),
        validation_presenter=validation_presenter or _ValidationPresenterStub(),
        audio_probe_flow=_AudioProbeFlowStub(),
        pipeline_state=pipeline_state,
        transitions=transitions,
        cuda_runtime_flow=object(),
        outcome_presenter=object(),
        assert_pipeline_thread=lambda: None,
        log_dialog_confirm_timing=lambda _output: None,
        launch_subtitle_generation=lambda run, options: launch_calls.append((run, options)),
        complete_run=lambda run_id, phase, close_progress: complete_calls.append((run_id, phase, close_progress)),
        request_active_task_stop=lambda: None,
    )
    return flow, pipeline_state, launch_calls, complete_calls


def test_start_flow_reopens_dialog_when_playback_context_changes_before_launch():
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/movie-a.mkv"
    player.playback._request_id = 7

    def _change_context():
        player.playback._media_path = "C:/media/movie-b.mkv"
        player.playback._request_id = 8

    flow, pipeline_state, launch_calls, complete_calls = _make_start_flow(
        player,
        preflight=_PreflightStub(_ValidationResult(), on_validate=_change_context),
    )

    flow.start(_options())

    assert launch_calls == []
    assert complete_calls == []
    assert pipeline_state.active_job is None
    assert pipeline_state.has_dialog_open() is True


def test_start_flow_cuda_cpu_fallback_launches_generation_with_cpu(monkeypatch):
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/movie.mkv"
    player.playback._request_id = 7
    flow, pipeline_state, launch_calls, complete_calls = _make_start_flow(player)

    monkeypatch.setattr(
        "services.subtitles.application.SubtitleGenerationStartFlow.get_missing_windows_cuda_runtime_packages",
        lambda: ["nvcuda"],
    )
    monkeypatch.setattr(
        "services.subtitles.application.SubtitleGenerationStartFlow.prompt_cuda_runtime_choice",
        lambda _parent, _packages: "cpu",
    )

    flow.start(_options(device="cuda"))

    assert complete_calls == []
    assert len(launch_calls) == 1
    assert launch_calls[0][1].device == "cpu"
    assert pipeline_state.active_job is not None
    assert pipeline_state.active_job.requested_options.device == "cuda"


def test_completion_flow_reports_context_change_without_auto_loading_subtitle():
    parent = QWidget()
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/other.mkv"
    player.playback._request_id = 99
    store = FakeMediaStore()
    media_library = MediaLibraryService(parent, player, store)
    outcome_presenter = _OutcomePresenterStub()
    pipeline_state = SubtitlePipelineStateMachine()
    transitions = SubtitlePipelineTransitions(pipeline_state)
    run = transitions.begin_run(
        generation_context=SubtitleGenerationContext("C:/media/original.mkv", 7),
        options=_options(),
    )

    complete_calls = []
    flow = SubtitleGenerationCompletionFlow(
        store=store,
        media_library=media_library,
        ui=_UiStub(),
        transitions=transitions,
        outcome_presenter=outcome_presenter,
        complete_run=lambda run_id, phase, close_progress: complete_calls.append((run_id, phase, close_progress)),
        launch_subtitle_generation=lambda run, options: None,
    )

    flow.handle_subtitle_generation_finished(run.run_id, "C:/media/movie.srt", True, False)

    assert complete_calls == [(run.run_id, SubtitlePipelinePhase.SUCCEEDED, True)]
    assert player.playback.opened_subtitles == []
    assert store.saved_last_open_dir == ["C:/media/movie.srt"]
    assert outcome_presenter.success_calls == [
        (
            "C:/media/movie.srt",
            SubtitleAutoOpenOutcome.CONTEXT_CHANGED,
            {
                "used_fallback_output_path": False,
                "requested_output_path": "C:/media/movie.srt",
            },
        )
    ]
