from PySide6.QtCore import QObject

from controllers.PlayerActionsController import PlayerActionsController
from tests.fakes import SignalRecorder


class _PlaybackStub(QObject):
    def __init__(self):
        super().__init__()
        self.assigned_media = False
        self.calls = []
        self.rate = 1.0
        self.volume = 50
        self.muted = False

    def has_assigned_media(self):
        return self.assigned_media

    def toggle_play_pause(self):
        self.calls.append(("toggle_play_pause",))

    def stop(self):
        self.calls.append(("stop",))

    def play_previous(self):
        self.calls.append(("play_previous",))

    def play_next(self):
        self.calls.append(("play_next",))

    def seek_by_hold(self, direction):
        self.calls.append(("seek_by_hold", direction))

    def begin_seek(self):
        self.calls.append(("begin_seek",))

    def seek_to_ratio(self, value):
        self.calls.append(("seek_to_ratio", value))

    def finish_seek(self):
        self.calls.append(("finish_seek",))

    def set_rate(self, speed):
        self.rate = max(0.25, min(4.0, float(speed)))
        self.calls.append(("set_rate", self.rate))

    def get_rate(self):
        return self.rate

    def set_volume(self, volume):
        self.volume = int(volume)
        self.calls.append(("set_volume", self.volume))

    def get_desired_volume(self):
        return self.volume

    def is_muted(self):
        return self.muted

    def toggle_mute(self):
        self.muted = not self.muted
        self.calls.append(("toggle_mute",))

    def seek_by_ms(self, delta_ms):
        self.calls.append(("seek_by_ms", delta_ms))


def test_actions_open_file_when_play_pause_has_no_media():
    playback = _PlaybackStub()
    controller = PlayerActionsController(playback, is_pip_active=lambda: False)
    open_file = SignalRecorder()
    controller.open_file_requested.connect(open_file)

    controller.on_play_pause()

    assert open_file.calls == [()]
    assert playback.calls == []


def test_actions_route_playback_and_view_mode_commands():
    playback = _PlaybackStub()
    playback.assigned_media = True
    pip_active = True
    controller = PlayerActionsController(playback, is_pip_active=lambda: pip_active)
    fullscreen = SignalRecorder()
    pip = SignalRecorder()
    pip_exit = SignalRecorder()
    controller.fullscreen_requested.connect(fullscreen)
    controller.pip_requested.connect(pip)
    controller.pip_exit_requested.connect(pip_exit)

    controller.on_play_pause()
    controller.on_stop()
    controller.on_fullscreen()
    controller.on_pip()
    controller.on_prev()
    controller.on_next()
    controller.on_seek_hold("right")
    controller.on_seek_started()
    controller.on_seek(0.4)
    controller.on_seek_finished()
    controller.seek_by_ms(-5000)

    assert playback.calls == [
        ("toggle_play_pause",),
        ("stop",),
        ("play_previous",),
        ("play_next",),
        ("seek_by_hold", "right"),
        ("begin_seek",),
        ("seek_to_ratio", 0.4),
        ("finish_seek",),
        ("seek_by_ms", -5000),
    ]
    assert fullscreen.calls == [()]
    assert pip.calls == [()]
    assert pip_exit.calls == [()]


def test_actions_adjust_speed_volume_and_mute_return_current_state():
    playback = _PlaybackStub()
    controller = PlayerActionsController(playback, is_pip_active=lambda: False)

    assert controller.on_speed_changed(1.5) == 1.5
    assert controller.adjust_speed(10.0) == 4.0
    assert controller.adjust_speed(-10.0) == 0.25
    assert controller.reset_speed() == 1.0
    assert controller.on_volume_changed(80) == (80, False)
    assert controller.adjust_volume(-10) == (70, False)
    assert controller.on_mute() == (70, True)
