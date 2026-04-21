from controllers.PlayerActionsController import PlayerActionsController
from controllers.PlayerPlaybackController import PlayerPlaybackController
from controllers.PlaybackViewStateController import PlaybackViewStateController
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


def test_stop_keeps_assigned_media_and_allows_play_on_same_media(workspace_tmp_path):
    controller = PlayerPlaybackController()
    actions = PlayerActionsController(controller, is_pip_active=lambda: False)
    media_path = _make_media_files(workspace_tmp_path, ["clip.mp4"])[0]
    open_requests = SignalRecorder()
    actions.open_file_requested.connect(open_requests)

    controller.open_paths([media_path])
    controller.engine.playing.emit(controller.current_request_id())
    controller.stop()

    assert controller.playback_state() == controller.STATE_STOPPED
    assert controller.has_media_loaded() is False
    assert controller.has_assigned_media() is True
    assert controller.current_media_path() == media_path
    assert controller.engine.stop_calls == 1

    actions.on_play_pause()

    assert open_requests.calls == []
    assert controller.engine.play_calls == 2


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


def test_play_after_confirmed_paused_media_does_not_enter_opening_view_semantics(workspace_tmp_path):
    controller = PlayerPlaybackController()
    view_state_controller = PlaybackViewStateController(controller)
    media_path = _make_media_files(workspace_tmp_path, ["resume.mp4"])[0]
    view_states = SignalRecorder()
    view_state_controller.view_state_changed.connect(view_states)

    controller.open_paths([media_path])
    controller.engine.playing.emit(controller.current_request_id())
    controller.pause()
    controller.engine.paused.emit(controller.current_request_id())

    assert controller.playback_state() == controller.STATE_PAUSED
    view_states.calls.clear()

    controller.play()

    assert controller.playback_state() == controller.STATE_PAUSED
    assert controller.engine.play_calls == 2
    assert view_states.calls == []

    paused_view_state = view_state_controller.current_view_state()
    assert paused_view_state.phase == controller.STATE_PAUSED
    assert paused_view_state.media_confirmed_loaded is True
    assert paused_view_state.placeholder_visible is False
    assert paused_view_state.progress_seekable is True
    assert paused_view_state.position_timer_active is True
    assert paused_view_state.play_pause_shows_playing is False


def test_resume_after_interruption_does_not_enter_opening_view_semantics(workspace_tmp_path):
    controller = PlayerPlaybackController()
    view_state_controller = PlaybackViewStateController(controller)
    media_path = _make_media_files(workspace_tmp_path, ["interrupted.mp4"])[0]
    view_states = SignalRecorder()
    view_state_controller.view_state_changed.connect(view_states)

    controller.open_paths([media_path])
    controller.engine.playing.emit(controller.current_request_id())
    assert controller.pause_for_interruption("subtitle_generation") is True
    controller.pause()
    controller.engine.paused.emit(controller.current_request_id())

    assert controller.playback_state() == controller.STATE_PAUSED
    view_states.calls.clear()

    controller.resume_after_interruption("subtitle_generation")

    assert controller.playback_state() == controller.STATE_PAUSED
    assert controller.engine.play_calls == 2
    assert view_states.calls == []

    paused_view_state = view_state_controller.current_view_state()
    assert paused_view_state.phase == controller.STATE_PAUSED
    assert paused_view_state.media_confirmed_loaded is True
    assert paused_view_state.placeholder_visible is False
    assert paused_view_state.progress_seekable is True
    assert paused_view_state.position_timer_active is True
    assert paused_view_state.play_pause_shows_playing is False


def test_new_media_open_enters_opening_view_semantics_before_first_confirmation(workspace_tmp_path):
    controller = PlayerPlaybackController()
    view_state_controller = PlaybackViewStateController(controller)
    media_path = _make_media_files(workspace_tmp_path, ["new.mp4"])[0]

    assert controller.open_paths([media_path]) is True

    opening_view_state = view_state_controller.current_view_state()
    assert opening_view_state.phase == controller.STATE_OPENING
    assert opening_view_state.media_assigned is True
    assert opening_view_state.media_confirmed_loaded is False
    assert opening_view_state.placeholder_visible is True
    assert opening_view_state.progress_seekable is False
    assert opening_view_state.position_timer_active is False
    assert opening_view_state.play_pause_shows_playing is False


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


def test_open_paths_playlist_skips_directory_item(workspace_tmp_path):
    controller = PlayerPlaybackController()
    directory_path = workspace_tmp_path / "folder.mp4"
    directory_path.mkdir()
    valid_path = _make_media_files(workspace_tmp_path, ["second.mp4"])[0]

    assert controller.open_paths([str(directory_path), valid_path]) is True
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


def test_open_paths_playlist_fails_when_all_items_are_directories(workspace_tmp_path):
    controller = PlayerPlaybackController()
    first_dir = workspace_tmp_path / "first.mp4"
    second_dir = workspace_tmp_path / "second.mp4"
    first_dir.mkdir()
    second_dir.mkdir()

    assert controller.open_paths([str(first_dir), str(second_dir)]) is False
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
