from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMainWindow

from controllers.PlayerPiPController import PiPController


class _FakeThemeState:
    def get(self, _name: str, default=None):
        return default


class _FakePlayback:
    def __init__(self):
        self._is_playing = False

    def is_playing(self) -> bool:
        return self._is_playing


class _FakePlayerWindow(QObject):
    video_host_ready = Signal()
    video_geometry_changed = Signal(int, int)

    def __init__(self):
        super().__init__()
        self.playback = _FakePlayback()
        self._video_host_ready = False
        self.bind_video_output_calls = 0
        self.play_calls = 0

    def is_pip_active(self) -> bool:
        return False

    def is_video_host_ready(self) -> bool:
        return self._video_host_ready

    def set_video_host_ready(self, ready: bool):
        self._video_host_ready = bool(ready)

    def bind_video_output(self):
        self.bind_video_output_calls += 1

    def play(self):
        self.play_calls += 1
        self.playback._is_playing = True


class _FakeHostWindow(QMainWindow):
    def init_pip_shortcuts(self, _pip_window):
        return None

    def sync_fullscreen_ui(self):
        return None


def _make_controller(player_window: _FakePlayerWindow) -> PiPController:
    host_window = _FakeHostWindow()
    return PiPController(
        host_window,
        player_window,
        metrics=None,
        theme_color=_FakeThemeState(),
    )


def test_rebind_resume_waits_for_valid_geometry_before_play():
    player_window = _FakePlayerWindow()
    player_window.set_video_host_ready(True)
    controller = _make_controller(player_window)

    controller._start_rebind_video_output_transition(resume_playback=True)

    assert player_window.bind_video_output_calls == 1
    assert player_window.play_calls == 0
    assert controller._pending_rebind_bound is True
    assert controller._awaiting_rebind_geometry is True

    player_window.video_geometry_changed.emit(0, 720)

    assert player_window.play_calls == 0
    assert controller._awaiting_rebind_geometry is True

    player_window.video_geometry_changed.emit(1280, 720)

    assert player_window.play_calls == 1
    assert player_window.playback.is_playing() is True
    assert controller._awaiting_rebind_geometry is False
    assert controller._pending_rebind_bound is False


def test_rebind_fallback_resumes_when_geometry_never_arrives(caplog):
    player_window = _FakePlayerWindow()
    player_window.set_video_host_ready(True)
    controller = _make_controller(player_window)

    with caplog.at_level(logging.INFO):
        controller._start_rebind_video_output_transition(resume_playback=True)
        controller._on_rebind_fallback_timeout()

    assert player_window.bind_video_output_calls == 1
    assert player_window.play_calls == 1
    assert controller._awaiting_rebind_geometry is False
    assert controller._pending_rebind_bound is False
    assert "PiP rebind fallback timeout without geometry" in caplog.text
    assert "PiP rebind resume via fallback" in caplog.text


def test_rebind_fallback_can_bind_after_late_host_ready(caplog):
    player_window = _FakePlayerWindow()
    controller = _make_controller(player_window)

    with caplog.at_level(logging.INFO):
        controller._start_rebind_video_output_transition(resume_playback=True)
        player_window.set_video_host_ready(True)
        controller._on_rebind_fallback_timeout()

    assert player_window.bind_video_output_calls == 1
    assert player_window.play_calls == 1
    assert "PiP rebind fallback bind completed" in caplog.text


def test_rebind_without_resume_completes_after_bind_only():
    player_window = _FakePlayerWindow()
    player_window.set_video_host_ready(True)
    controller = _make_controller(player_window)

    controller._start_rebind_video_output_transition(resume_playback=False)

    assert player_window.bind_video_output_calls == 1
    assert player_window.play_calls == 0
    assert controller._awaiting_rebind_geometry is False
    assert controller._pending_rebind_bound is False
    assert controller._rebind_fallback_timer.isActive() is False


def test_rebind_stale_geometry_does_not_resume_new_transition():
    player_window = _FakePlayerWindow()
    player_window.set_video_host_ready(True)
    controller = _make_controller(player_window)

    controller._start_rebind_video_output_transition(resume_playback=True)
    stale_transition_id = controller._pending_transition_id
    controller._start_rebind_video_output_transition(resume_playback=True)

    controller._on_video_geometry_changed(stale_transition_id, 1280, 720)

    assert player_window.bind_video_output_calls == 2
    assert player_window.play_calls == 0
    assert controller._awaiting_rebind_geometry is True

    player_window.video_geometry_changed.emit(1280, 720)

    assert player_window.play_calls == 1
    assert controller._awaiting_rebind_geometry is False


def test_rebind_geometry_resume_does_not_allow_duplicate_fallback_play():
    player_window = _FakePlayerWindow()
    player_window.set_video_host_ready(True)
    controller = _make_controller(player_window)

    controller._start_rebind_video_output_transition(resume_playback=True)
    player_window.video_geometry_changed.emit(1280, 720)
    controller._on_rebind_fallback_timeout()

    assert player_window.play_calls == 1
