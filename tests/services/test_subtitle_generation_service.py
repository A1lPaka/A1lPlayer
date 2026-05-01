import inspect

from models.SubtitleGenerationDialogResult import SubtitleGenerationDialogResult
from services.media.MediaLibraryService import MediaLibraryService
from services.subtitles.facade.SubtitleGenerationService import SubtitleGenerationService
from services.subtitles.state.SubtitlePipelineState import (
    SubtitleGenerationContext,
    SubtitlePipelinePhase,
    SubtitlePipelineTask,
)
from tests.fakes import FakeSubtitleWorker


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


def _make_service(parent, player, store):
    media_library = MediaLibraryService(parent, player, store)
    return SubtitleGenerationService(parent, player, store, media_library)


def test_service_public_facade_is_stable():
    public_methods = {
        name
        for name, value in SubtitleGenerationService.__dict__.items()
        if inspect.isfunction(value) and not name.startswith("_")
    }

    assert public_methods == {
        "begin_emergency_shutdown",
        "begin_force_shutdown",
        "begin_shutdown",
        "generate_subtitle",
        "has_active_tasks",
        "is_shutdown_in_progress",
    }
    assert hasattr(SubtitleGenerationService, "shutdown_finished")


def test_generate_subtitle_requires_loaded_media(qt_parent, fake_player_window, fake_media_store):
    service = _make_service(qt_parent, fake_player_window, fake_media_store)

    assert service.generate_subtitle() is False


def test_generate_subtitle_opens_dialog_and_requests_audio_probe(
    monkeypatch,
    qt_parent,
    fake_player_window,
    fake_media_store,
):
    player = fake_player_window
    player.playback._media_path = "C:/media/movie.mkv"
    player.playback._request_id = 7
    player.playback._has_media_loaded = True
    service = _make_service(qt_parent, player, fake_media_store)
    dialog_requests = []
    audio_probe_requests = []
    takeover_calls = []

    monkeypatch.setattr(
        service._runtime.playback_takeover,
        "acquire",
        lambda: takeover_calls.append(True),
    )
    monkeypatch.setattr(
        service._ui,
        "open_generation_dialog",
        lambda media_path, on_generate, on_cancel: dialog_requests.append(
            {
                "media_path": media_path,
                "on_generate": on_generate,
                "on_cancel": on_cancel,
            }
        ),
    )
    monkeypatch.setattr(
        service._audio_probe_flow,
        "load_generation_audio_tracks_async",
        lambda media_path: audio_probe_requests.append(media_path),
    )

    assert service.generate_subtitle() is True
    assert takeover_calls == [True]
    assert dialog_requests[0]["media_path"] == "C:/media/movie.mkv"
    assert callable(dialog_requests[0]["on_generate"])
    assert callable(dialog_requests[0]["on_cancel"])
    assert audio_probe_requests == ["C:/media/movie.mkv"]


def test_generate_subtitle_focuses_existing_dialog_instead_of_reopening(
    monkeypatch,
    qt_parent,
    fake_player_window,
    fake_media_store,
):
    player = fake_player_window
    player.playback._media_path = "C:/media/movie.mkv"
    player.playback._request_id = 7
    player.playback._has_media_loaded = True
    service = _make_service(qt_parent, player, fake_media_store)
    focus_calls = []
    already_running_calls = []

    monkeypatch.setattr(service._ui, "focus_active_dialog", lambda: focus_calls.append(True))
    monkeypatch.setattr(service._pipeline_state, "has_dialog_open", lambda: True)
    monkeypatch.setattr(
        "services.subtitles.facade.SubtitleGenerationService.show_subtitle_generation_already_running",
        lambda _parent: already_running_calls.append(True),
    )

    assert service.generate_subtitle() is False
    assert focus_calls == [True]
    assert already_running_calls == []


def test_generate_subtitle_rejects_reentry_while_background_task_runs(
    monkeypatch,
    qt_parent,
    fake_player_window,
    fake_media_store,
):
    player = fake_player_window
    player.playback._media_path = "C:/media/movie.mkv"
    player.playback._request_id = 7
    player.playback._has_media_loaded = True
    service = _make_service(qt_parent, player, fake_media_store)
    focus_calls = []
    already_running_calls = []

    monkeypatch.setattr(service._ui, "focus_active_dialog", lambda: focus_calls.append(True))
    monkeypatch.setattr(service._pipeline_state, "blocks_new_generation_request", lambda: True)
    monkeypatch.setattr(
        "services.subtitles.facade.SubtitleGenerationService.show_subtitle_generation_already_running",
        lambda _parent: already_running_calls.append(True),
    )

    assert service.generate_subtitle() is False
    assert focus_calls == [True]
    assert already_running_calls == [True]


def test_subtitle_terminal_event_survives_thread_cleanup(
    monkeypatch,
    qt_parent,
    fake_player_window,
    fake_media_store,
):
    service = _make_service(qt_parent, fake_player_window, fake_media_store)
    worker = object()
    thread = object()
    terminal_calls = []

    run = service._transitions.begin_run(
        SubtitleGenerationContext("C:/media/movie.mkv", 7),
        _options(),
    )
    run.phase = SubtitlePipelinePhase.RUNNING
    run.task = SubtitlePipelineTask.SUBTITLE_GENERATION
    run.subtitle_thread = thread
    run.subtitle_worker = worker
    service._runtime.pending_subtitle_thread_run_ids.add(run.run_id)
    monkeypatch.setattr(
        service._completion_flow,
        "handle_subtitle_generation_finished",
        lambda run_id, output_path, auto_open, fallback: terminal_calls.append(
            (run_id, output_path, auto_open, fallback)
        ),
    )

    service._runtime.on_background_task_thread_finished(run.run_id, SubtitlePipelineTask.SUBTITLE_GENERATION)
    service._on_subtitle_generation_finished_from_worker(run.run_id, worker, "C:/media/movie.srt", True, False)

    assert terminal_calls == [(run.run_id, "C:/media/movie.srt", True, False)]


def test_subtitle_progress_event_uses_active_worker_identity(
    monkeypatch,
    qt_parent,
    fake_player_window,
    fake_media_store,
):
    service = _make_service(qt_parent, fake_player_window, fake_media_store)
    worker = object()
    progress_calls = []

    run = service._transitions.begin_run(
        SubtitleGenerationContext("C:/media/movie.mkv", 7),
        _options(),
    )
    run.phase = SubtitlePipelinePhase.RUNNING
    run.task = SubtitlePipelineTask.SUBTITLE_GENERATION
    run.subtitle_thread = object()
    run.subtitle_worker = worker
    monkeypatch.setattr(service._ui, "update_progress", progress_calls.append)

    service._on_worker_progress_changed_from_worker(run.run_id, worker, 42)

    assert progress_calls == [42]


def test_subtitle_worker_stale_terminal_identity_is_ignored(
    monkeypatch,
    qt_parent,
    fake_player_window,
    fake_media_store,
):
    service = _make_service(qt_parent, fake_player_window, fake_media_store)
    active_worker = object()
    stale_worker = object()
    terminal_calls = []

    run = service._transitions.begin_run(
        SubtitleGenerationContext("C:/media/movie.mkv", 7),
        _options(),
    )
    run.phase = SubtitlePipelinePhase.RUNNING
    run.task = SubtitlePipelineTask.SUBTITLE_GENERATION
    run.subtitle_thread = object()
    run.subtitle_worker = active_worker
    monkeypatch.setattr(
        service._completion_flow,
        "handle_subtitle_generation_failed",
        lambda run_id, error, diagnostics: terminal_calls.append((run_id, error, diagnostics)),
    )

    service._on_subtitle_generation_failed_from_worker(run.run_id, stale_worker, "failed", "diagnostics")

    assert terminal_calls == []


def test_subtitle_worker_start_abort_releases_ui_suspend_lease(
    qt_parent,
    fake_player_window,
    fake_media_store,
):
    service = _make_service(qt_parent, fake_player_window, fake_media_store)
    worker = object()
    thread = object()
    run = service._transitions.begin_run(
        SubtitleGenerationContext("C:/media/movie.mkv", 7),
        _options(),
    )
    run.phase = SubtitlePipelinePhase.RUNNING
    run.task = SubtitlePipelineTask.SUBTITLE_GENERATION
    run.subtitle_thread = thread
    run.subtitle_worker = worker

    service._runtime.suspend_player_ui_for_generation()
    service._runtime.on_subtitle_worker_start_aborted(run.run_id, thread, worker)

    assert fake_player_window.suspend_leases[-1].released is True
    assert service._runtime.player_ui_suspend_lease is None
    assert run.subtitle_thread is None
    assert run.subtitle_worker is None


def test_shutdown_completes_after_subtitle_worker_terminal_event(
    qt_parent,
    fake_player_window,
    fake_media_store,
):
    service = _make_service(qt_parent, fake_player_window, fake_media_store)
    shutdown_calls = []
    service.shutdown_finished.connect(lambda: shutdown_calls.append(True))
    worker = FakeSubtitleWorker()
    thread = object()

    run = service._transitions.begin_run(
        SubtitleGenerationContext("C:/media/movie.mkv", 7),
        _options(),
    )
    run.phase = SubtitlePipelinePhase.RUNNING
    run.task = SubtitlePipelineTask.SUBTITLE_GENERATION
    run.subtitle_thread = thread
    run.subtitle_worker = worker
    service._runtime.pending_subtitle_thread_run_ids.add(run.run_id)

    assert service.begin_shutdown() is True
    service._on_subtitle_generation_canceled_from_worker(run.run_id, worker)

    assert shutdown_calls == []

    service._runtime.on_background_task_thread_finished(run.run_id, SubtitlePipelineTask.SUBTITLE_GENERATION)

    assert shutdown_calls == [True]
