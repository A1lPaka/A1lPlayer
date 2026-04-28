import inspect

from services.media.MediaLibraryService import MediaLibraryService
from services.subtitles.facade.SubtitleGenerationService import SubtitleGenerationService


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
