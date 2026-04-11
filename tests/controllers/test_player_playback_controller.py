from controllers.PlayerPlaybackController import PlayerPlaybackController

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


def test_exit_after_current_emits_close_request_and_stops_playback(workspace_tmp_path):
    controller = PlayerPlaybackController()
    media_path = _make_media_files(workspace_tmp_path, ["final.mp4"])[0]
    finished = SignalRecorder()
    close_requests = SignalRecorder()
    controller.media_finished.connect(finished)
    controller.exit_after_current_requested.connect(close_requests)
    controller.set_exit_after_current(True)

    controller.open_paths([media_path])
    controller.engine.playing.emit(controller.current_request_id())
    controller.engine.media_ended.emit(controller.current_request_id())

    assert finished.calls == [(media_path,)]
    assert close_requests.calls == [()]
    assert controller.playback_state() == controller.STATE_STOPPED
    assert controller.engine.stop_calls == 1
