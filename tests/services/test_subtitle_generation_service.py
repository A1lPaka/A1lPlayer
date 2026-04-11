from PySide6.QtWidgets import QWidget

from services.subtitles.SubtitleGenerationService import (
    SubtitleGenerationContext,
    SubtitleGenerationService,
    SubtitleGenerationState,
)
from ui.SubtitleGenerationDialog import SubtitleGenerationDialogResult

from tests.fakes import FakePlayerWindow, FakeSubtitleWorker, FakeMediaStore


class _RunningThread:
    def isRunning(self):
        return True


def _options(output_path: str = "C:/tmp/subtitles.srt") -> SubtitleGenerationDialogResult:
    return SubtitleGenerationDialogResult(
        audio_stream_index=None,
        audio_language=None,
        device=None,
        model_size="small",
        output_format="srt",
        output_path=output_path,
        auto_open_after_generation=True,
    )


def _seed_active_run(service: SubtitleGenerationService, media_path: str = "C:/media/movie.mkv", request_id: int = 7):
    service._state = SubtitleGenerationState.RUNNING
    service._active_run = service._begin_pipeline_run(
        SubtitleGenerationContext(media_path=media_path, request_id=request_id),
        _options(),
    )
    return service._active_run


def test_generation_starts_from_idle_and_rejects_reentry(monkeypatch):
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/movie.mkv"
    player.playback._request_id = 7
    parent = QWidget()
    service = SubtitleGenerationService(parent, player, FakeMediaStore())

    launches = []
    already_running_calls = []

    def fake_launch(run, options):
        launches.append((run, options))
        service._state = SubtitleGenerationState.RUNNING

    monkeypatch.setattr(service, "_launch_subtitle_generation", fake_launch)
    monkeypatch.setattr(
        "services.subtitles.SubtitleGenerationService.show_subtitle_generation_already_running",
        lambda _parent: already_running_calls.append(True),
    )

    assert service.generate_subtitle() is True
    assert service._state == SubtitleGenerationState.DIALOG_OPEN
    assert service._ui.dialog_requests[-1]["media_path"] == "C:/media/movie.mkv"

    service._ui.dialog_requests[-1]["on_generate"](_options())

    assert len(launches) == 1
    assert launches[0][0].context.media_path == "C:/media/movie.mkv"
    # This test stubs the real launch path, so deferred UI suspend is not expected here.
    assert player.suspend_calls == 0
    assert service._active_run is not None

    assert service.generate_subtitle() is False
    assert already_running_calls == [True]
    assert service._ui.focus_calls == 1


def test_cancel_transitions_to_canceling_and_is_idempotent():
    player = FakePlayerWindow()
    service = SubtitleGenerationService(QWidget(), player, FakeMediaStore())
    worker = FakeSubtitleWorker()

    _seed_active_run(service)
    service._subtitle_worker = worker

    service._cancel_subtitle_generation()
    service._cancel_subtitle_generation()

    assert service._state == SubtitleGenerationState.CANCELING
    assert worker.cancel_calls == 1
    assert service._ui.cancel_pending_calls == 1


def test_begin_shutdown_requests_graceful_stop_for_active_worker():
    player = FakePlayerWindow()
    service = SubtitleGenerationService(QWidget(), player, FakeMediaStore())
    worker = FakeSubtitleWorker()

    _seed_active_run(service)
    service._subtitle_worker = worker
    service._subtitle_thread = _RunningThread()

    pending = service.begin_shutdown()

    assert pending is True
    assert service._state == SubtitleGenerationState.SHUTTING_DOWN
    assert worker.cancel_calls == 1
    assert service._ui.closed_generation_dialogs == 1
    assert service.is_shutdown_in_progress() is True


def test_begin_force_shutdown_requests_force_stop_for_active_worker():
    player = FakePlayerWindow()
    service = SubtitleGenerationService(QWidget(), player, FakeMediaStore())
    worker = FakeSubtitleWorker()

    _seed_active_run(service)
    service._subtitle_worker = worker
    service._subtitle_thread = _RunningThread()

    pending = service.begin_force_shutdown()

    assert pending is True
    assert service._state == SubtitleGenerationState.SHUTTING_DOWN
    assert worker.force_stop_calls == 1
    assert service._ui.closed_progress_dialogs == 1


def test_stale_run_events_are_ignored():
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/movie.mkv"
    player.playback._request_id = 7
    service = SubtitleGenerationService(QWidget(), player, FakeMediaStore())

    current_run = _seed_active_run(service)

    service._on_worker_progress_changed(current_run.run_id + 1, 42)
    service._on_subtitle_generation_finished(current_run.run_id + 1, "C:/tmp/out.srt", True, False)

    assert service._ui.progress_updates == []
    assert service._active_run is current_run
    assert player.resume_calls == 0


def test_terminal_completion_clears_active_run_and_resumes_player_ui():
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/movie.mkv"
    player.playback._request_id = 7
    store = FakeMediaStore()
    service = SubtitleGenerationService(QWidget(), player, store)

    run = _seed_active_run(service)
    service._ensure_player_ui_suspended()

    service._on_subtitle_generation_finished(run.run_id, "C:/tmp/generated.srt", True, False)

    assert service._state == SubtitleGenerationState.SUCCEEDED
    assert service._active_run is None
    assert player.resume_calls == 1
    assert player.playback.opened_subtitles == ["C:/tmp/generated.srt"]
    assert store.saved_last_open_dir == ["C:/tmp/generated.srt"]
    assert service._outcomes.successes
