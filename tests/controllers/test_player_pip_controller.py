from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMainWindow

from controllers.ViewModeController import ViewModeController


class _FakeThemeState:
    def get(self, _name: str, default=None):
        return default


class _FakePlayback(QObject):
    media_finished = Signal(str)
    video_geometry_changed = Signal(int, int)

    def __init__(self):
        super().__init__()
        self._is_playing = False
        self._interruptions = {}
        self.pause_calls = 0
        self.play_calls = 0
        self.view_modes_allowed = True
        self.video_dimensions = (1280, 720)

    def is_playing(self) -> bool:
        return self._is_playing

    def pause(self):
        self.pause_calls += 1
        self._is_playing = False

    def play(self):
        self.play_calls += 1
        self._is_playing = True

    def can_activate_view_modes(self) -> bool:
        return self.view_modes_allowed

    def get_video_dimensions(self):
        return self.video_dimensions

    def pause_for_interruption(self, owner: str, *, emit_pause_requested: bool = True):
        interruption = self._interruptions.get(owner)
        if interruption is not None:
            return interruption["paused_by_owner"]

        paused_by_owner = self._is_playing
        self._interruptions[owner] = {
            "paused_by_owner": paused_by_owner,
            "emit_pause_requested": emit_pause_requested,
        }
        return paused_by_owner

    def resume_after_interruption(self, owner: str):
        interruption = self._interruptions.pop(owner, None)
        if interruption is None or not interruption["paused_by_owner"]:
            return
        if self._interruptions or self._is_playing:
            return
        self.play()

    def clear_interruption(self, owner: str):
        self._interruptions.pop(owner, None)

    def create_interruption_lease(self, owner: str, *, emit_pause_requested: bool = True):
        return _FakePlaybackInterruptionLease(
            self,
            owner,
            emit_pause_requested=emit_pause_requested,
        )


class _FakePlaybackInterruptionLease:
    def __init__(self, playback: _FakePlayback, owner: str, *, emit_pause_requested: bool = True):
        self._playback = playback
        self._owner = owner
        self._emit_pause_requested = emit_pause_requested
        self._acquired = False
        self._paused_playback = False

    @property
    def paused_playback(self) -> bool:
        return self._paused_playback

    def acquire(self) -> bool:
        if self._acquired:
            return self._paused_playback

        self._acquired = True
        self._paused_playback = self._playback.pause_for_interruption(
            self._owner,
            emit_pause_requested=self._emit_pause_requested,
        )
        if self._paused_playback:
            self._playback.pause()
        return self._paused_playback

    def release(self, *, resume_playback: bool = True):
        if not self._acquired:
            return

        self._acquired = False
        self._paused_playback = False
        if resume_playback:
            self._playback.resume_after_interruption(self._owner)
            return

        self._playback.clear_interruption(self._owner)


class _FakePlayerWindow(QObject):
    video_host_ready = Signal()

    def __init__(self):
        super().__init__()
        self.playback = _FakePlayback()
        self._video_host_ready = False
        self._pip_active = False
        self._chrome_hidden = False
        self.bind_video_output_calls = 0

    def is_pip_active(self) -> bool:
        return self._pip_active

    def set_pip_active(self, active: bool):
        self._pip_active = bool(active)

    def is_video_host_ready(self) -> bool:
        return self._video_host_ready

    def set_video_host_ready(self, ready: bool):
        self._video_host_ready = bool(ready)

    def bind_video_output(self):
        self.bind_video_output_calls += 1

    def set_fullscreen_mode(self, _fullscreen: bool):
        return None

    def is_chrome_hidden(self) -> bool:
        return self._chrome_hidden

class _FakeHostWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.restored_player_widget = None
        self.player_widget_to_take = None
        self.take_player_widget_calls = 0
        self.show_normal_calls = 0
        self.raise_calls = 0
        self.activate_calls = 0

    def init_pip_shortcuts(self, _pip_window):
        return None

    def _take_player_window_for_view_mode(self):
        self.take_player_widget_calls += 1
        player_widget = self.player_widget_to_take
        self.player_widget_to_take = None
        return player_widget

    def _restore_player_window_from_view_mode(self, player_window):
        self.restored_player_widget = player_window

    def showNormal(self):
        self.show_normal_calls += 1

    def raise_(self):
        self.raise_calls += 1

    def activateWindow(self):
        self.activate_calls += 1


def _make_controller(player_window: _FakePlayerWindow) -> ViewModeController:
    host_window = _FakeHostWindow()
    return ViewModeController(
        host_window,
        player_window,
        metrics=None,
        theme_color=_FakeThemeState(),
    )


def _prepare_rebind_resume(controller: ViewModeController, player_window: _FakePlayerWindow):
    player_window.playback._is_playing = True
    assert controller._rebind_lease.acquire() is True


def test_rebind_resume_waits_for_valid_geometry_before_play():
    player_window = _FakePlayerWindow()
    player_window.set_video_host_ready(True)
    controller = _make_controller(player_window)
    _prepare_rebind_resume(controller, player_window)

    controller._start_rebind_video_output_transition()

    assert player_window.bind_video_output_calls == 1
    assert player_window.playback.play_calls == 0
    assert controller._pending_rebind_bound is True
    assert controller._awaiting_rebind_geometry is True

    player_window.playback.video_geometry_changed.emit(0, 720)

    assert player_window.playback.play_calls == 0
    assert controller._awaiting_rebind_geometry is True

    player_window.playback.video_geometry_changed.emit(1280, 720)

    assert player_window.playback.play_calls == 1
    assert player_window.playback.is_playing() is True
    assert controller._awaiting_rebind_geometry is False
    assert controller._pending_rebind_bound is False


def test_initial_bind_is_owned_by_controller_via_video_host_ready_signal():
    player_window = _FakePlayerWindow()
    controller = _make_controller(player_window)

    player_window.video_host_ready.emit()

    assert player_window.bind_video_output_calls == 0

    player_window.set_video_host_ready(True)
    player_window.video_host_ready.emit()

    assert player_window.bind_video_output_calls == 1
    assert controller._initial_video_output_bound is True

    player_window.video_host_ready.emit()

    assert player_window.bind_video_output_calls == 1


def test_rebind_fallback_resumes_when_geometry_never_arrives(caplog):
    player_window = _FakePlayerWindow()
    player_window.set_video_host_ready(True)
    controller = _make_controller(player_window)
    _prepare_rebind_resume(controller, player_window)

    with caplog.at_level(logging.INFO):
        controller._start_rebind_video_output_transition()
        controller._on_rebind_fallback_timeout()

    assert player_window.bind_video_output_calls == 1
    assert player_window.playback.play_calls == 1
    assert controller._awaiting_rebind_geometry is False
    assert controller._pending_rebind_bound is False
    assert "PiP rebind fallback timeout without geometry" in caplog.text
    assert "PiP rebind resume via fallback" in caplog.text


def test_rebind_fallback_can_bind_after_late_host_ready(caplog):
    player_window = _FakePlayerWindow()
    controller = _make_controller(player_window)
    _prepare_rebind_resume(controller, player_window)

    with caplog.at_level(logging.INFO):
        controller._start_rebind_video_output_transition()
        player_window.set_video_host_ready(True)
        controller._on_rebind_fallback_timeout()

    assert player_window.bind_video_output_calls == 1
    assert player_window.playback.play_calls == 1
    assert "PiP rebind fallback bind completed" in caplog.text


def test_rebind_without_resume_completes_after_bind_only():
    player_window = _FakePlayerWindow()
    player_window.set_video_host_ready(True)
    controller = _make_controller(player_window)

    controller._start_rebind_video_output_transition()

    assert player_window.bind_video_output_calls == 1
    assert player_window.playback.play_calls == 0
    assert controller._awaiting_rebind_geometry is False
    assert controller._pending_rebind_bound is False
    assert controller._rebind_fallback_timer.isActive() is False


def test_rebind_stale_geometry_does_not_resume_new_transition():
    player_window = _FakePlayerWindow()
    player_window.set_video_host_ready(True)
    controller = _make_controller(player_window)
    _prepare_rebind_resume(controller, player_window)

    controller._start_rebind_video_output_transition()
    stale_transition_id = controller._pending_transition_id
    controller._start_rebind_video_output_transition()

    controller._on_video_geometry_changed(stale_transition_id, 1280, 720)

    assert player_window.bind_video_output_calls == 2
    assert player_window.playback.play_calls == 0
    assert controller._awaiting_rebind_geometry is True

    player_window.playback.video_geometry_changed.emit(1280, 720)

    assert player_window.playback.play_calls == 1
    assert controller._awaiting_rebind_geometry is False


def test_rebind_geometry_resume_does_not_allow_duplicate_fallback_play():
    player_window = _FakePlayerWindow()
    player_window.set_video_host_ready(True)
    controller = _make_controller(player_window)
    _prepare_rebind_resume(controller, player_window)

    controller._start_rebind_video_output_transition()
    player_window.playback.video_geometry_changed.emit(1280, 720)
    controller._on_rebind_fallback_timeout()

    assert player_window.playback.play_calls == 1


class _FakePiPWindow:
    def __init__(self, player_widget=None):
        self._player_widget = player_widget
        self.hide_calls = 0
        self.show_calls = 0
        self.raise_calls = 0
        self.activate_calls = 0
        self.aspect_ratio_updates = []

    def setCentralWidget(self, player_widget):
        self._player_widget = player_widget

    def takeCentralWidget(self):
        player_widget = self._player_widget
        self._player_widget = None
        return player_widget

    def set_video_aspect_ratio(self, width, height):
        self.aspect_ratio_updates.append((width, height))

    def show(self):
        self.show_calls += 1

    def raise_(self):
        self.raise_calls += 1

    def activateWindow(self):
        self.activate_calls += 1

    def hide(self):
        self.hide_calls += 1


def test_enter_pip_uses_view_mode_host_adapter_and_starts_rebind(monkeypatch):
    player_window = _FakePlayerWindow()
    controller = _make_controller(player_window)
    fake_pip_window = _FakePiPWindow()
    controller._pip_window = fake_pip_window
    controller._host_window.player_widget_to_take = player_window
    rebind_calls = []

    monkeypatch.setattr(
        controller,
        "_start_rebind_video_output_transition",
        lambda: rebind_calls.append(controller._rebind_lease.paused_playback),
    )

    controller.enter_pip()

    assert controller._host_window.take_player_widget_calls == 1
    assert fake_pip_window._player_widget is player_window
    assert player_window.is_pip_active() is True
    assert fake_pip_window.aspect_ratio_updates == [(1280, 720)]
    assert fake_pip_window.show_calls == 1
    assert fake_pip_window.raise_calls == 1
    assert fake_pip_window.activate_calls == 1
    assert rebind_calls == [False]


def test_exit_pip_restores_host_window_and_starts_rebind(monkeypatch):
    player_window = _FakePlayerWindow()
    player_window.set_pip_active(True)
    player_window.set_video_host_ready(True)
    controller = _make_controller(player_window)
    fake_pip_window = _FakePiPWindow(player_window)
    controller._pip_window = fake_pip_window
    rebind_calls = []

    monkeypatch.setattr(
        controller,
        "_start_rebind_video_output_transition",
        lambda: rebind_calls.append(controller._rebind_lease.paused_playback),
    )

    controller.exit_pip()

    assert controller._host_window.restored_player_widget is player_window
    assert player_window.is_pip_active() is False
    assert fake_pip_window.hide_calls == 1
    assert controller._host_window.show_normal_calls == 1
    assert controller._host_window.raise_calls == 1
    assert controller._host_window.activate_calls == 1
    assert rebind_calls == [False]


def test_toggle_pip_enters_and_exits_through_single_controller_owner(monkeypatch):
    player_window = _FakePlayerWindow()
    controller = _make_controller(player_window)
    enter_calls = []
    exit_calls = []

    monkeypatch.setattr(controller, "enter_pip", lambda: enter_calls.append(True))
    monkeypatch.setattr(controller, "exit_pip", lambda: exit_calls.append(True))

    controller.toggle_pip()
    player_window.set_pip_active(True)
    controller.toggle_pip()

    assert enter_calls == [True]
    assert exit_calls == [True]


def test_enter_pip_is_gated_by_view_mode_availability():
    player_window = _FakePlayerWindow()
    player_window.playback.view_modes_allowed = False
    controller = _make_controller(player_window)

    controller.enter_pip()

    assert controller.is_active() is False
    assert controller._pip_window is None


def test_teardown_for_shutdown_restores_ownership_without_interactive_restore(monkeypatch):
    player_window = _FakePlayerWindow()
    player_window.set_pip_active(True)
    controller = _make_controller(player_window)
    fake_pip_window = _FakePiPWindow(player_window)
    controller._pip_window = fake_pip_window
    controller._pending_rebind_bound = True
    controller._awaiting_rebind_geometry = True
    controller._rebind_fallback_timer.start()
    rebind_calls = []

    monkeypatch.setattr(controller, "_start_rebind_video_output_transition", lambda: rebind_calls.append(controller._rebind_lease.paused_playback))

    controller.teardown_for_shutdown()

    assert controller._host_window.restored_player_widget is player_window
    assert player_window.is_pip_active() is False
    assert fake_pip_window.hide_calls == 1
    assert controller._host_window.show_normal_calls == 0
    assert controller._host_window.raise_calls == 0
    assert controller._host_window.activate_calls == 0
    assert rebind_calls == []
    assert controller._pending_rebind_bound is False
    assert controller._awaiting_rebind_geometry is False
    assert controller._rebind_fallback_timer.isActive() is False


def test_media_finished_exits_active_pip():
    player_window = _FakePlayerWindow()
    controller = _make_controller(player_window)
    exit_calls = []

    controller.is_active = lambda: True
    controller.exit_pip = lambda: exit_calls.append(True)

    player_window.playback.media_finished.emit("final.mp4")

    assert exit_calls == [True]


def test_media_finished_does_not_exit_when_pip_is_inactive():
    player_window = _FakePlayerWindow()
    controller = _make_controller(player_window)
    exit_calls = []

    controller.is_active = lambda: False
    controller.exit_pip = lambda: exit_calls.append(True)

    player_window.playback.media_finished.emit("final.mp4")

    assert exit_calls == []
