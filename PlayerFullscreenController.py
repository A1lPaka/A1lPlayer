from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QAbstractAnimation, QEasingCurve, QEvent, QObject, QPoint, QPropertyAnimation, QTimer, Qt
from PySide6.QtWidgets import QWidget

from utils import Metrics


class PlayerFullscreenController(QObject):
    _CONTROLS_HIDE_DELAY_MS = 2000
    _CONTROLS_ANIMATION_DURATION_MS = 180
    _SINGLE_CLICK_DELAY_MS = 220

    def __init__(
        self,
        host: QWidget,
        video_frame: QWidget,
        controls: QWidget,
        time_popup: QWidget,
        *,
        has_media_loaded: Callable[[], bool],
        toggle_play_pause: Callable[[], None],
        request_fullscreen: Callable[[], None],
    ):
        super().__init__(host)
        self._host = host
        self._video_frame = video_frame
        self._controls = controls
        self._time_popup = time_popup
        self._has_media_loaded = has_media_loaded
        self._toggle_play_pause = toggle_play_pause
        self._request_fullscreen = request_fullscreen

        self._metrics: Metrics | None = None
        self._is_fullscreen = False
        self._controls_visible = True
        self._controls_animation_target_visible = True
        self._cursor_hidden = False
        self._pending_fullscreen_controls_show = False

        self._controls_hide_timer = QTimer(self)
        self._controls_hide_timer.setSingleShot(True)
        self._controls_hide_timer.setInterval(self._CONTROLS_HIDE_DELAY_MS)
        self._controls_hide_timer.timeout.connect(self._hide_controls_if_idle)

        self._controls_animation = QPropertyAnimation(self._controls, b"pos", self)
        self._controls_animation.setDuration(self._CONTROLS_ANIMATION_DURATION_MS)
        self._controls_animation.setEasingCurve(QEasingCurve.OutCubic)
        self._controls_animation.finished.connect(self._on_controls_animation_finished)

        self._single_click_timer = QTimer(self)
        self._single_click_timer.setSingleShot(True)
        self._single_click_timer.setInterval(self._SINGLE_CLICK_DELAY_MS)
        self._single_click_timer.timeout.connect(self._trigger_video_single_click)

        self._enable_mouse_tracking(self._host)

    def apply_metrics(self, metrics: Metrics):
        self._metrics = metrics
        self.update_layout()

    def handle_resize(self):
        self.update_layout()
        if self._pending_fullscreen_controls_show and self._is_fullscreen:
            self._pending_fullscreen_controls_show = False
            QTimer.singleShot(0, self._show_controls_after_fullscreen_ready)

    def set_fullscreen_mode(self, fullscreen: bool):
        self._is_fullscreen = bool(fullscreen)
        self._controls_animation.stop()
        self._controls_hide_timer.stop()

        if self._is_fullscreen:
            self._pending_fullscreen_controls_show = True
            self._set_controls_visible(False, animate=False)
            self.update_layout()
            return

        self._pending_fullscreen_controls_show = False
        self._set_controls_visible(True, animate=False)
        self.update_layout()

    def is_fullscreen(self) -> bool:
        return self._is_fullscreen

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.MouseMove, QEvent.Enter):
            self._handle_pointer_activity()
        elif (
            event.type() == QEvent.MouseButtonPress
            and event.button() == Qt.LeftButton
            and self._is_video_area_widget(watched)
        ):
            self._single_click_timer.start()
        elif (
            event.type() == QEvent.MouseButtonDblClick
            and event.button() == Qt.LeftButton
            and self._is_video_area_widget(watched)
        ):
            self._single_click_timer.stop()
            self._request_fullscreen()
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def update_layout(self):
        if self._metrics is None:
            return

        width = self._host.width()
        height = self._host.height()
        controls_height = self._controls_height()

        if self._is_fullscreen:
            self._video_frame.setGeometry(0, 0, width, height)
            controls_y = self._controls.y()
            if self._controls_animation.state() != QAbstractAnimation.Running:
                controls_y = self._controls_target_y(self._controls_visible)
            self._controls.setGeometry(0, controls_y, width, controls_height)
            self._controls.setVisible(self._controls_visible)
            self._controls.raise_()
            return

        video_height = max(0, height - controls_height)
        self._video_frame.setGeometry(0, 0, width, video_height)
        self._controls.setGeometry(0, video_height, width, controls_height)
        self._controls.show()
        self._controls.raise_()

    def _enable_mouse_tracking(self, widget: QWidget):
        widget.setMouseTracking(True)
        widget.installEventFilter(self)
        for child in widget.findChildren(QWidget):
            child.setMouseTracking(True)
            child.installEventFilter(self)

    def _handle_pointer_activity(self):
        if not self._is_fullscreen:
            return
        self._set_controls_visible(True, animate=True)
        self._controls_hide_timer.start()

    def _is_video_area_widget(self, widget: QWidget) -> bool:
        return widget is self._video_frame or self._video_frame.isAncestorOf(widget)

    def _trigger_video_single_click(self):
        if not self._has_media_loaded():
            return
        self._toggle_play_pause()

    def _show_controls_after_fullscreen_ready(self):
        if not self._is_fullscreen:
            return
        self.update_layout()
        self._set_controls_visible(True, animate=True)
        self._controls_hide_timer.start()

    def _hide_controls_if_idle(self):
        if not self._is_fullscreen:
            return
        if self._controls.underMouse():
            self._controls_hide_timer.start()
            return
        self._set_controls_visible(False, animate=True)

    def _controls_target_y(self, visible: bool) -> int:
        return self._host.height() - self._controls_height() if visible else self._host.height()

    def _controls_height(self) -> int:
        assert self._metrics is not None
        return max(60, int(self._metrics.icon_size * 3.5))

    def _set_controls_visible(self, visible: bool, *, animate: bool):
        self._controls_visible = visible
        self._set_cursor_hidden(self._is_fullscreen and not visible)
        if not visible and self._time_popup.isVisible():
            self._time_popup.hide()
        if animate and self._is_fullscreen:
            self._animate_controls(visible)
            return
        self._controls.setVisible(visible or not self._is_fullscreen)

    def _animate_controls(self, show: bool):
        if not self._is_fullscreen:
            return

        self._controls_animation.stop()
        self._controls_animation_target_visible = show
        self._controls.raise_()

        if show:
            if not self._controls.isVisible():
                self._controls.show()
            start_y = self._controls.y()
            visible_y = self._controls_target_y(True)
            hidden_y = self._controls_target_y(False)
            if start_y < visible_y or start_y > hidden_y:
                start_y = hidden_y
            end_y = visible_y
        else:
            if not self._controls.isVisible():
                return
            start_y = self._controls.y()
            end_y = self._controls_target_y(False)

        self._controls_animation.setStartValue(QPoint(0, start_y))
        self._controls_animation.setEndValue(QPoint(0, end_y))
        self._controls_animation.start()

    def _on_controls_animation_finished(self):
        if self._controls_animation_target_visible:
            self._controls.show()
            return
        if self._is_fullscreen and not self._controls.underMouse():
            self._controls.hide()

    def _set_cursor_hidden(self, hidden: bool):
        if self._cursor_hidden == hidden:
            return
        if hidden:
            cursor = Qt.BlankCursor
            self._host.setCursor(cursor)
            self._video_frame.setCursor(cursor)
            self._controls.setCursor(cursor)
        else:
            self._host.unsetCursor()
            self._video_frame.unsetCursor()
            self._controls.unsetCursor()
        self._cursor_hidden = hidden
