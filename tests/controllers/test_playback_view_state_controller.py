from PySide6.QtCore import QObject, Signal

from controllers.PlaybackViewStateController import PlaybackViewStateController
from controllers.PlayerPlaybackController import PlayerPlaybackController
from tests.fakes import SignalRecorder


class _PlaybackStub(QObject):
    playback_state_changed = Signal(str)
    playback_error = Signal(int, str, str)

    def __init__(self):
        super().__init__()
        self.phase = PlayerPlaybackController.STATE_STOPPED
        self.assigned = False
        self.loaded = False

    def playback_state(self):
        return self.phase

    def has_assigned_media(self):
        return self.assigned

    def has_media_loaded(self):
        return self.loaded


def test_view_state_blocks_media_dependent_ui_without_loaded_media():
    playback = _PlaybackStub()
    controller = PlaybackViewStateController(playback)

    state = controller.current_view_state()

    assert state.placeholder_visible is True
    assert state.progress_seekable is False
    assert state.position_timer_active is False
    assert state.play_pause_shows_playing is False


def test_view_state_transitions_for_opening_playing_and_paused():
    playback = _PlaybackStub()
    controller = PlaybackViewStateController(playback)
    changes = SignalRecorder()
    controller.view_state_changed.connect(changes)

    playback.assigned = True
    playback.phase = PlayerPlaybackController.STATE_OPENING
    playback.playback_state_changed.emit(playback.phase)
    opening = controller.current_view_state()

    playback.loaded = True
    playback.phase = PlayerPlaybackController.STATE_PLAYING
    playback.playback_state_changed.emit(playback.phase)
    playing = controller.current_view_state()

    playback.phase = PlayerPlaybackController.STATE_PAUSED
    playback.playback_state_changed.emit(playback.phase)
    paused = controller.current_view_state()

    assert opening.placeholder_visible is True
    assert opening.progress_seekable is False
    assert playing.placeholder_visible is False
    assert playing.progress_seekable is True
    assert playing.position_timer_active is True
    assert playing.play_pause_shows_playing is True
    assert paused.placeholder_visible is False
    assert paused.progress_seekable is True
    assert paused.play_pause_shows_playing is False
    assert len(changes.calls) == 3


def test_view_state_forces_error_emission_even_when_state_is_unchanged():
    playback = _PlaybackStub()
    controller = PlaybackViewStateController(playback)
    changes = SignalRecorder()
    errors = SignalRecorder()
    controller.view_state_changed.connect(changes)
    controller.playback_error.connect(errors)

    playback.playback_error.emit(7, "broken.mp4", "failed")

    assert len(changes.calls) == 1
    assert errors.calls == [("broken.mp4", "failed")]
