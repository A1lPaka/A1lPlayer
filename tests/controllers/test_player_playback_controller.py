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

    controller.engine.set_time(250)
    controller.engine.set_length(1000)
    controller.engine.playing.emit(controller.current_request_id())

    assert controller.has_media_loaded() is True
    assert controller.playback_state() == controller.STATE_PLAYING
    assert confirmed.calls == [(controller.current_request_id(), media_path)]
    assert controller.get_session_snapshot() == {
        "path": media_path,
        "position_ms": 250,
        "total_ms": 1000,
    }


def test_playback_error_resets_confirmed_state_and_emits_error(workspace_tmp_path):
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


def test_stop_resets_playback_to_stopped(workspace_tmp_path):
    controller = PlayerPlaybackController()
    media_path = _make_media_files(workspace_tmp_path, ["clip.mp4"])[0]

    controller.open_paths([media_path])
    controller.engine.playing.emit(controller.current_request_id())
    controller.stop()

    assert controller.playback_state() == controller.STATE_STOPPED
    assert controller.has_media_loaded() is False
    assert controller.engine.stop_calls == 1


def test_nested_playback_interruptions_resume_only_when_last_owner_releases(workspace_tmp_path):
    controller = PlayerPlaybackController()
    media_path = _make_media_files(workspace_tmp_path, ["nested.mp4"])[0]

    controller.open_paths([media_path])
    controller.engine.playing.emit(controller.current_request_id())

    assert controller.pause_for_interruption("subtitle_generation") is True
    controller.engine.pause()
    assert controller.pause_for_interruption("pip_rebind") is False

    controller.resume_after_interruption("pip_rebind")

    assert controller.engine.play_calls == 1
    assert controller.engine.is_playing() is False

    controller.resume_after_interruption("subtitle_generation")

    assert controller.engine.play_calls == 2
    assert controller.engine.is_playing() is True


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


def test_media_end_with_playlist_advance_loads_next_item(workspace_tmp_path):
    controller = PlayerPlaybackController()
    first_path, second_path = _make_media_files(workspace_tmp_path, ["first.mp4", "second.mp4"])

    controller.open_paths([first_path, second_path])
    first_request_id = controller.current_request_id()
    controller.engine.playing.emit(first_request_id)
    controller.engine.media_ended.emit(first_request_id)

    assert controller.current_media_path() == second_path
    assert controller.engine.loaded_media == [first_path, second_path]
    assert controller.playback_state() == controller.STATE_OPENING


def test_playlist_state_load_keeps_missing_paths_without_eager_validation():
    playlist = PlaylistState()
    missing_path = "Z:/definitely-missing/movie.mp4"

    assert playlist.load([missing_path], start_index=0) is True
    assert playlist.paths == [missing_path]
    assert playlist.current_index == 0


def test_open_paths_single_missing_file_fails_strictly():
    controller = PlayerPlaybackController()
    missing_path = "Z:/missing/solo.mp4"

    assert controller.open_paths([missing_path]) is False
    assert controller.current_media_path() is None
    assert controller.engine.loaded_media == []


def test_open_paths_playlist_skips_missing_current_item(workspace_tmp_path):
    controller = PlayerPlaybackController()
    valid_path = _make_media_files(workspace_tmp_path, ["second.mp4"])[0]
    missing_path = "Z:/missing/first.mp4"

    assert controller.open_paths([missing_path, valid_path]) is True
    assert controller.current_media_path() == valid_path
    assert controller.engine.loaded_media == [valid_path]


def test_open_paths_playlist_fails_when_all_items_are_missing():
    controller = PlayerPlaybackController()
    missing_paths = [
        "Z:/missing/first.mp4",
        "Z:/missing/second.mp4",
    ]

    assert controller.open_paths(missing_paths) is False
    assert controller.current_media_path() is None
    assert controller.engine.loaded_media == []


def test_play_next_skips_missing_items(workspace_tmp_path):
    controller = PlayerPlaybackController()
    first_path, third_path = _make_media_files(workspace_tmp_path, ["first.mp4", "third.mp4"])
    missing_path = "Z:/missing/second.mp4"

    assert controller.open_paths([first_path, missing_path, third_path]) is True
    assert controller.play_next() is True
    assert controller.current_media_path() == third_path
    assert controller.engine.loaded_media == [first_path, third_path]


def test_play_previous_skips_missing_items(workspace_tmp_path):
    controller = PlayerPlaybackController()
    first_path, third_path = _make_media_files(workspace_tmp_path, ["first.mp4", "third.mp4"])
    missing_path = "Z:/missing/second.mp4"

    assert controller.open_paths([first_path, missing_path, third_path]) is True
    assert controller.play_previous() is True
    assert controller.current_media_path() == third_path
    assert controller.engine.loaded_media == [first_path, third_path]


def test_media_end_skips_missing_items_during_linear_advance(workspace_tmp_path):
    controller = PlayerPlaybackController()
    first_path, third_path = _make_media_files(workspace_tmp_path, ["first.mp4", "third.mp4"])
    missing_path = "Z:/missing/second.mp4"

    assert controller.open_paths([first_path, missing_path, third_path]) is True
    first_request_id = controller.current_request_id()
    controller.engine.playing.emit(first_request_id)
    controller.engine.media_ended.emit(first_request_id)

    assert controller.current_media_path() == third_path
    assert controller.engine.loaded_media == [first_path, third_path]
    assert controller.playback_state() == controller.STATE_OPENING


def test_media_end_can_be_stopped_by_upper_owner_before_playlist_advance(workspace_tmp_path):
    controller = PlayerPlaybackController()
    first_path, second_path = _make_media_files(workspace_tmp_path, ["final.mp4", "next.mp4"])
    finished = SignalRecorder()
    controller.media_finished.connect(finished)
    controller.media_finished.connect(lambda _path: controller.stop())

    controller.open_paths([first_path, second_path])
    controller.engine.playing.emit(controller.current_request_id())
    controller.engine.media_ended.emit(controller.current_request_id())

    assert finished.calls == [(first_path,)]
    assert controller.playback_state() == controller.STATE_STOPPED
    assert controller.current_media_path() == first_path
    assert controller.engine.loaded_media == [first_path]
    assert controller.engine.stop_calls == 1


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
