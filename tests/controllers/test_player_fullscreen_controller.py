from PySide6.QtWidgets import QWidget

from controllers.PlayerFullscreenController import PlayerFullscreenController


class _ControlsStub(QWidget):
    def __init__(self, parent=None, height=48):
        super().__init__(parent)
        self._preferred_height = height
        self.raise_calls = 0

    def preferred_height(self):
        return self._preferred_height

    def raise_(self):
        self.raise_calls += 1
        super().raise_()


class _AcceptEvent:
    def __init__(self):
        self.accepted = False

    def accept(self):
        self.accepted = True


def _make_controller(*, has_media_loaded=True):
    host = QWidget()
    host.resize(640, 360)
    video_frame = QWidget(host)
    controls = _ControlsStub(host)
    time_popup = QWidget(host)
    calls = {"toggle": 0, "fullscreen": 0}

    controller = PlayerFullscreenController(
        host,
        video_frame,
        controls,
        time_popup,
        has_media_loaded=lambda: has_media_loaded,
        toggle_play_pause=lambda: calls.__setitem__("toggle", calls["toggle"] + 1),
        request_fullscreen=lambda: calls.__setitem__("fullscreen", calls["fullscreen"] + 1),
    )
    return controller, host, video_frame, controls, time_popup, calls


def _detach_controller(controller, host):
    host.removeEventFilter(controller)
    for child in host.findChildren(QWidget):
        child.removeEventFilter(controller)


def test_fullscreen_controller_normal_layout_and_fullscreen_restore():
    controller, host, video_frame, controls, _time_popup, _calls = _make_controller()

    try:
        controller.update_layout()

        assert video_frame.geometry().height() == host.height() - controls.preferred_height()
        assert controls.isHidden() is False

        controller.set_fullscreen_mode(True)

        assert controller.is_fullscreen() is True
        assert video_frame.geometry().height() == host.height()
        assert controls.isHidden() is True

        controller.set_fullscreen_mode(False)

        assert controller.is_fullscreen() is False
        assert video_frame.geometry().height() == host.height() - controls.preferred_height()
        assert controls.isHidden() is False
    finally:
        _detach_controller(controller, host)


def test_fullscreen_controller_forced_hidden_restores_when_released():
    controller, host, video_frame, controls, _time_popup, _calls = _make_controller()

    try:
        controller.set_controls_forced_hidden(True)

        assert video_frame.geometry().height() == host.height()
        assert controls.isHidden() is True

        controller.set_controls_forced_hidden(False)

        assert video_frame.geometry().height() == host.height() - controls.preferred_height()
        assert controls.isHidden() is False
    finally:
        _detach_controller(controller, host)


def test_fullscreen_controller_video_double_click_requests_toggle_outside_pip():
    controller, host, *_rest, calls = _make_controller()
    event = _AcceptEvent()

    try:
        handled = controller._handle_video_mouse_double_click(event)

        assert handled is True
        assert event.accepted is True
        assert calls["fullscreen"] == 1
    finally:
        _detach_controller(controller, host)


def test_fullscreen_controller_single_click_toggles_play_only_with_loaded_media():
    controller, host, *_rest, calls = _make_controller(has_media_loaded=True)

    try:
        controller._trigger_video_single_click()

        assert calls["toggle"] == 1
    finally:
        _detach_controller(controller, host)

    controller, host, *_rest, calls = _make_controller(has_media_loaded=False)

    try:
        controller._trigger_video_single_click()

        assert calls["toggle"] == 0
    finally:
        _detach_controller(controller, host)
