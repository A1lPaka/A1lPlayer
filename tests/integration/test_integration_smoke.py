import importlib.util
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings

from models.SubtitleGenerationDialogResult import SubtitleGenerationDialogResult
from models.ThemeColor import ThemeState
from services.app.AppCloseCoordinator import AppCloseCoordinator
from services.app.MediaSettingsStore import MediaSettingsStore
from services.media.MediaLibraryService import MediaLibraryService
from services.media.MediaPathService import MediaPathService
from services.subtitles.domain.SubtitleTypes import AudioStreamInfo
from services.subtitles.facade.SubtitleGenerationService import SubtitleGenerationService
from services.subtitles.validation.SubtitleGenerationPreflight import AudioStreamProbeState
from tests.fakes import FakeSubtitleService

pytestmark = pytest.mark.integration


def _load_real_playback_engine_module():
    module_path = Path(__file__).parents[2] / "services" / "playback" / "PlaybackEngine.py"
    spec = importlib.util.spec_from_file_location("integration_real_playback_engine", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _settings(workspace_tmp_path):
    return QSettings(str(workspace_tmp_path / "settings.ini"), QSettings.IniFormat)


def test_key_services_import_and_construct_with_real_modules(qt_parent, fake_player_window, workspace_tmp_path):
    playback_module = _load_real_playback_engine_module()
    parent = qt_parent
    player = fake_player_window
    player.theme_color = ThemeState()
    store = MediaSettingsStore(_settings(workspace_tmp_path))

    media_paths = MediaPathService()
    media_library = MediaLibraryService(parent, player, store)
    subtitle_service = SubtitleGenerationService(parent, player, store, media_library)
    close = AppCloseCoordinator(
        parent,
        subtitle_service,
        media_library,
        shutdown_playback=lambda: None,
        is_pip_active=lambda: False,
        teardown_pip_for_shutdown=lambda: None,
    )

    assert playback_module.PlaybackService is not None
    media_path = workspace_tmp_path / "movie.mp4"
    subtitle_path = workspace_tmp_path / "movie.srt"
    media_path.write_text("media", encoding="utf-8")
    subtitle_path.write_text("subtitle", encoding="utf-8")
    assert media_paths.cheap_classify_drag_paths([str(media_path), str(subtitle_path)]) == {
        "media_paths": [str(media_path)],
        "subtitle_paths": [str(subtitle_path)],
    }
    assert subtitle_service.has_active_tasks() is False
    assert close.attempt_close().can_close is True


def test_open_confirm_save_session_and_close_application_flow(qt_parent, fake_player_window, workspace_tmp_path):
    parent = qt_parent
    player = fake_player_window
    store = MediaSettingsStore(_settings(workspace_tmp_path))
    media_library = MediaLibraryService(parent, player, store)
    subtitle_service = FakeSubtitleService()
    playback_shutdown_calls = []
    media_path = str(workspace_tmp_path / "movie.mp4")
    Path(media_path).write_text("media", encoding="utf-8")

    close = AppCloseCoordinator(
        parent,
        subtitle_service,
        media_library,
        shutdown_playback=lambda: playback_shutdown_calls.append(True),
        is_pip_active=lambda: False,
        teardown_pip_for_shutdown=lambda: None,
    )

    assert media_library.open_media_paths([media_path]) is True

    request_id = player.playback.current_request_id()
    player.playback._media_path = media_path
    player.playback._has_media_loaded = True
    player.playback._session_snapshot = {
        "path": media_path,
        "position_ms": 12_000,
        "total_ms": 60_000,
    }
    player.playback.media_confirmed.emit(request_id, media_path)

    media_library.save_time_session()
    result = close.attempt_close()

    assert result.can_close is True
    assert result.shutdown_completed is True
    assert store.get_recent_media() == [media_path]
    assert store.get_saved_position(media_path) == 12_000
    assert playback_shutdown_calls == [True]


def test_subtitle_generation_service_flow_without_real_worker_or_vlc(
    monkeypatch,
    qt_parent,
    fake_player_window,
    workspace_tmp_path,
):
    parent = qt_parent
    player = fake_player_window
    player.theme_color = ThemeState()
    media_path = str(workspace_tmp_path / "movie.mkv")
    output_path = str(workspace_tmp_path / "movie.srt")
    Path(media_path).write_text("media", encoding="utf-8")
    player.playback._media_path = media_path
    player.playback._request_id = 42
    player.playback._has_media_loaded = True
    store = MediaSettingsStore(_settings(workspace_tmp_path))
    media_library = MediaLibraryService(parent, player, store)
    service = SubtitleGenerationService(parent, player, store, media_library)
    runner_calls = []

    monkeypatch.setattr(service._audio_probe_flow, "load_generation_audio_tracks_async", lambda _media_path: None)
    monkeypatch.setattr(service._audio_probe_flow, "probe_state_for_media", lambda _media_path: AudioStreamProbeState.READY)
    monkeypatch.setattr(
        service._audio_probe_flow,
        "get_cached_audio_streams_for_media",
        lambda _media_path: [AudioStreamInfo(stream_index=2, label="Audio 2 | ENG")],
    )
    monkeypatch.setattr(service._audio_probe_flow, "get_cached_audio_stream_error_for_media", lambda _media_path: None)
    monkeypatch.setattr(service._subtitle_job_runner, "start", lambda run, options: runner_calls.append((run, options)))

    assert service.generate_subtitle() is True

    options = SubtitleGenerationDialogResult(
        audio_stream_index=2,
        audio_language="en",
        device="cpu",
        model_size="small",
        output_format="srt",
        output_path=output_path,
        auto_open_after_generation=True,
    )
    service._start_flow.start(options)

    assert len(runner_calls) == 1
    run, launched_options = runner_calls[0]
    assert launched_options == options
    assert service.has_active_tasks() is True

    service._completion_flow.handle_subtitle_generation_finished(run.run_id, output_path, True, False)
    service._on_subtitle_worker_thread_finished(run.run_id)

    assert service.has_active_tasks() is False
    assert player.playback.opened_subtitles == [output_path]
    assert store.get_last_open_dir() == str(workspace_tmp_path.resolve())
