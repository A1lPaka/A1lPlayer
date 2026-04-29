from controllers.PlayerPlaybackController import PlayerPlaybackController
from models.PlaybackPlaylist import PlaylistState

from tests.fakes import SignalRecorder


def _make_media_files(tmp_path, names):
    paths = []
    for name in names:
        path = tmp_path / name
        path.write_text("media")
        paths.append(str(path))
    return paths


def test_open_paths_assigns_media_and_session_snapshot_waits_for_confirmation(workspace_tmp_path):
    controller = PlayerPlaybackController()
    media_path = _make_media_files(workspace_tmp_path, ["movie.mp4"])[0]
    assigned = SignalRecorder()
    confirmed = SignalRecorder()
    controller.media_assigned.connect(assigned)
    controller.media_confirmed.connect(confirmed)

    assert controller.open_paths([media_path], start_position_ms=250) is True
    assert controller.has_assigned_media() is True
    assert controller.has_media_loaded() is False
    assert controller.current_media_path() == media_path
    assert controller.get_session_snapshot() is None
    assert assigned.calls == [(media_path,)]

    controller.engine.set_length(1000)
    controller.engine.playing.emit(controller.current_request_id())

    assert controller.has_media_loaded() is True
    assert controller.playback_state() == controller.STATE_PLAYING
    assert confirmed.calls == [(controller.current_request_id(), media_path)]
    assert controller.get_session_snapshot() == {
        "path": media_path,
        "position_ms": 0,
        "total_ms": 1000,
    }


def test_open_paths_passes_start_position_to_engine_load(workspace_tmp_path):
    controller = PlayerPlaybackController()
    media_path = _make_media_files(workspace_tmp_path, ["movie.mp4"])[0]
    load_calls = []
    original_load_media = controller.engine.load_media

    def track_load_media(path, start_position_ms=0):
        load_calls.append((path, start_position_ms))
        return original_load_media(path, start_position_ms=start_position_ms)

    controller.engine.load_media = track_load_media

    assert controller.open_paths([media_path], start_position_ms=250) is True

    assert load_calls == [(media_path, 250)]


def test_start_position_is_consumed_before_playlist_next_load(workspace_tmp_path):
    controller = PlayerPlaybackController()
    first_path, second_path = _make_media_files(workspace_tmp_path, ["first.mp4", "second.mp4"])
    load_calls = []
    original_load_media = controller.engine.load_media

    def track_load_media(path, start_position_ms=0):
        load_calls.append((path, start_position_ms))
        return original_load_media(path, start_position_ms=start_position_ms)

    controller.engine.load_media = track_load_media

    assert controller.open_paths([first_path, second_path], start_position_ms=250) is True
    assert controller.play_next() is True

    assert load_calls == [(first_path, 250), (second_path, 0)]


def test_fatal_playback_error_clears_assigned_media_and_emits_error(workspace_tmp_path):
    controller = PlayerPlaybackController()
    media_path = _make_media_files(workspace_tmp_path, ["broken.mp4"])[0]
    errors = SignalRecorder()
    controller.playback_error.connect(errors)

    controller.open_paths([media_path])
    controller.engine.playing.emit(controller.current_request_id())
    controller.engine.playback_error.emit(controller.current_request_id(), media_path, "boom")

    assert controller.has_media_loaded() is False
    assert controller.has_assigned_media() is False
    assert controller.current_request_id() == 0
    assert controller.playback_state() == controller.STATE_STOPPED
    assert errors.calls == [(1, media_path, "boom")]


def test_engine_stopped_clears_confirmed_playback_but_keeps_assigned_media(workspace_tmp_path):
    controller = PlayerPlaybackController()
    media_path = _make_media_files(workspace_tmp_path, ["engine-stop.mp4"])[0]

    controller.open_paths([media_path])
    controller.engine.playing.emit(controller.current_request_id())
    controller.engine.stopped.emit(controller.current_request_id())

    assert controller.playback_state() == controller.STATE_STOPPED
    assert controller.has_media_loaded() is False
    assert controller.has_assigned_media() is True
    assert controller.current_media_path() == media_path


def test_resume_after_interruption_only_resumes_after_last_owner_releases(workspace_tmp_path):
    controller = PlayerPlaybackController()
    media_path = _make_media_files(workspace_tmp_path, ["interrupted.mp4"])[0]

    controller.open_paths([media_path])
    controller.engine.playing.emit(controller.current_request_id())
    assert controller.pause_for_interruption("subtitle_generation") is True
    controller.pause()
    assert controller.pause_for_interruption("pip_rebind") is False

    controller.resume_after_interruption("pip_rebind")
    assert controller.engine.play_calls == 1
    controller.resume_after_interruption("subtitle_generation")
    assert controller.engine.play_calls == 2


def test_new_media_load_resets_confirmed_active_state_and_assigns_new_media(workspace_tmp_path):
    controller = PlayerPlaybackController()
    first_path, second_path = _make_media_files(workspace_tmp_path, ["old.mp4", "new.mp4"])
    active_media_changes = SignalRecorder()
    controller.active_media_changed.connect(active_media_changes)

    controller.open_paths([first_path, second_path])
    first_request_id = controller.current_request_id()
    controller.engine.playing.emit(first_request_id)
    active_media_changes.calls.clear()

    assert controller.play_next() is True

    assert controller.current_media_path() == second_path
    assert controller.current_request_id() != first_request_id
    assert controller.has_assigned_media() is True
    assert controller.has_media_loaded() is False
    assert controller.playback_state() == controller.STATE_OPENING
    assert active_media_changes.calls == [(None,)]


def test_media_end_without_next_item_stops_playback(workspace_tmp_path):
    controller = PlayerPlaybackController()
    media_path = _make_media_files(workspace_tmp_path, ["solo.mp4"])[0]
    finished = SignalRecorder()
    controller.media_finished.connect(finished)

    controller.open_paths([media_path])
    controller.engine.playing.emit(controller.current_request_id())
    controller.engine.media_ended.emit(controller.current_request_id())

    assert finished.calls == [(media_path,)]
    assert controller.playback_state() == controller.STATE_STOPPED
    assert controller.current_media_path() == media_path
    assert controller.engine.stop_calls == 1


def test_playlist_state_load_keeps_missing_paths_without_eager_validation():
    playlist = PlaylistState()
    missing_path = "Z:/definitely-missing/movie.mp4"

    assert playlist.load([missing_path], start_index=0) is True
    assert playlist.paths == [missing_path]
    assert playlist.current_index == 0


def test_playlist_state_load_rejects_empty_and_non_string_paths():
    playlist = PlaylistState()

    assert playlist.load(["", "   ", None], start_index=0) is False
    assert playlist.paths == []
    assert playlist.current_index == -1


def test_playlist_state_load_filters_invalid_paths_without_eager_validation():
    playlist = PlaylistState()
    valid_path = "Z:/offline/movie.mp4"

    assert playlist.load(["", None, valid_path], start_index=0) is True
    assert playlist.paths == [valid_path]
    assert playlist.current_index == 0


def test_playlist_navigation_and_media_end_skip_unplayable_items(workspace_tmp_path):
    controller = PlayerPlaybackController()
    first_path, third_path = _make_media_files(workspace_tmp_path, ["first.mp4", "third.mp4"])
    missing_path = "Z:/missing/second.mp4"

    assert controller.open_paths([first_path, missing_path, third_path]) is True
    assert controller.play_next() is True
    assert controller.current_media_path() == third_path
    assert controller.play_previous() is True
    assert controller.current_media_path() == first_path
    first_request_id = controller.current_request_id()
    controller.engine.playing.emit(first_request_id)
    controller.engine.media_ended.emit(first_request_id)

    assert controller.current_media_path() == third_path
    assert controller.engine.loaded_media[0] == first_path
    assert controller.engine.loaded_media[-1] == third_path
    assert controller.playback_state() == controller.STATE_OPENING


def test_shutdown_is_idempotent_and_resets_controller_state(workspace_tmp_path):
    controller = PlayerPlaybackController()
    media_path = _make_media_files(workspace_tmp_path, ["shutdown.mp4"])[0]

    controller.open_paths([media_path], start_position_ms=250)
    controller.engine.playing.emit(controller.current_request_id())

    controller.shutdown()
    controller.shutdown()

    assert controller.playback_state() == controller.STATE_STOPPED
    assert controller.has_media_loaded() is False
    assert controller.has_assigned_media() is False
    assert controller.current_request_id() == 0
    assert controller.engine.shutdown_calls == 1
