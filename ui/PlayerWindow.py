from PySide6.QtCore import Qt, QTimer, Signal, QPoint, QEvent
from PySide6.QtGui import QPalette, QColor, QCursor
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid

from ui.AnimatedVideoPlaceholder import AnimatedVideoPlaceholder
from controllers.PlayerActionsController import PlayerActionsController
from controllers.PlayerFullscreenController import PlayerFullscreenController
from controllers.PlayerPlaybackController import PlayerPlaybackController
from controllers.PlaybackViewStateController import PlaybackViewState, PlaybackViewStateController
from ui.PlayerControls import PlayerControls, TimePopup, SpeedPopup
from utils import Metrics
from models.ThemeColor import ThemeState


class SubtitleGenerationUiSuspendLease:
    def __init__(self, player_window: "PlayerWindow", *, acquired: bool):
        self._player_window = player_window
        self._acquired = acquired

    def release(self):
        if not self._acquired:
            return
        self._acquired = False
        self._player_window._resume_subtitle_generation_ui_suspend()

    def resume(self):
        self.release()


class PlayerWindow(QWidget):
    open_file_requested = Signal()
    media_finished = Signal(str)
    media_assigned = Signal(str)
    current_media_changed = Signal(str)
    active_media_changed = Signal(object)
    playback_error = Signal(str, str)
    video_geometry_changed = Signal(int, int)
    video_host_ready = Signal()
    fullscreen_requested = Signal()
    pip_requested = Signal()
    pip_exit_requested = Signal()
    SPEED_POPUP_AUTOHIDE_MS = 4000

    def __init__(self, metrics: Metrics | None, theme_color: ThemeState):
        super().__init__()
        self.setAttribute(Qt.WA_StyledBackground, True)

        self.metrics = metrics
        self.theme_color = theme_color
        self.playback = PlayerPlaybackController(self)
        self.player_actions = PlayerActionsController(
            self.playback,
            is_pip_active=self.is_pip_active,
            parent=self,
        )
        self.playback_view_state = PlaybackViewStateController(self.playback, self)

        self._pip_active = False
        self._chrome_hidden = False
        self._subtitle_generation_ui_suspended = False
        self._subtitle_generation_timer_was_active = False

        self._init_video_frame()
        self._init_controls()
        self._init_audio()
        self._init_timer()

    def _init_video_frame(self):
        self.video_frame = QWidget(self)
        self.video_frame.setAttribute(Qt.WA_NativeWindow, True)
        self.video_frame.setMouseTracking(True)
        self.video_frame.setAutoFillBackground(True)
        palette = self.video_frame.palette()
        palette.setColor(QPalette.Window, QColor(0, 0, 0))
        self.video_frame.setPalette(palette)
        self.video_frame.winId()

        self.video_placeholder = AnimatedVideoPlaceholder(self.video_frame, self.metrics)
        self.video_placeholder.show_placeholder()

    def _init_controls(self):
        self.controls = PlayerControls(self, self.metrics, self.theme_color)

        self.controls.play_pause_button.clicked.connect(self.player_actions.on_play_pause)
        self.controls.stop_button.clicked.connect(self.player_actions.on_stop)
        self.controls.progress_bar.seek_started.connect(self.player_actions.on_seek_started)
        self.controls.progress_bar.value_changed.connect(self.player_actions.on_seek)
        self.controls.progress_bar.seek_finished.connect(self.player_actions.on_seek_finished)
        self.controls.progress_bar.hover_changed.connect(self.on_progress_hover_changed)
        self.controls.progress_bar.hover_left.connect(self.on_progress_hover_left)
        self.controls.volume_controls.volume_bar.volume_changed.connect(
            lambda volume: self.on_volume_changed(int(volume * 100))
        )
        self.controls.volume_controls.volume_button.clicked.connect(self.on_mute)
        self.controls.fullscreen_button.clicked.connect(self.player_actions.on_fullscreen)
        self.controls.pip_button.clicked.connect(self.player_actions.on_pip)
        self.controls.rewind_lbutton.clicked.connect(self.player_actions.on_prev)
        self.controls.rewind_rbutton.clicked.connect(self.player_actions.on_next)
        self.controls.rewind_lbutton.seek_hold.connect(self.player_actions.on_seek_hold)
        self.controls.rewind_rbutton.seek_hold.connect(self.player_actions.on_seek_hold)

        self.time_popup = TimePopup(None, metrics=self.metrics, theme_color=self.theme_color)
        self.time_popup.hide()
        self.speed_popup = SpeedPopup(None, metrics=self.metrics, theme_color=self.theme_color)
        self.speed_popup.set_speed(self.playback.get_rate())
        self.controls.set_speed_value(self.playback.get_rate())
        self.speed_popup.hide()

        self.speed_popup_autohide_timer = QTimer(self)
        self.speed_popup_autohide_timer.setSingleShot(True)
        self.speed_popup_autohide_timer.setInterval(self.SPEED_POPUP_AUTOHIDE_MS)
        self.speed_popup_autohide_timer.timeout.connect(self._on_speed_popup_autohide_timeout)

        self.controls.speed_button.clicked.connect(self.toggle_speed_popup)
        self.controls.speed_label.clicked.connect(self.toggle_speed_popup)
        self.speed_popup.speed_changed.connect(self.on_speed_changed)
        self._install_local_event_filters(self)
        self._install_local_event_filters(self.video_frame)
        self._install_local_event_filters(self.controls)
        self.speed_popup.installEventFilter(self)

        self.playback.media_assigned.connect(self.media_assigned.emit)
        self.playback.active_media_changed.connect(self.active_media_changed.emit)
        self.playback.current_media_changed.connect(self._on_current_media_changed)
        self.playback.media_finished.connect(self.media_finished.emit)
        self.playback.video_geometry_changed.connect(self.video_geometry_changed.emit)
        self.playback_view_state.view_state_changed.connect(self._apply_playback_view_state)
        self.playback_view_state.playback_error.connect(self.playback_error.emit)
        self.player_actions.open_file_requested.connect(self.open_file_requested.emit)
        self.player_actions.fullscreen_requested.connect(self.fullscreen_requested.emit)
        self.player_actions.pip_requested.connect(self.pip_requested.emit)
        self.player_actions.pip_exit_requested.connect(self.pip_exit_requested.emit)

        self.fullscreen_controller = PlayerFullscreenController(
            self,
            self.video_frame,
            self.controls,
            self.time_popup,
            has_media_loaded=self.playback.has_media_loaded,
            toggle_play_pause=self.player_actions.on_play_pause,
            request_fullscreen=self.player_actions.on_fullscreen,
        )
        self.fullscreen_controller.apply_metrics()

    def _init_audio(self):
        initial_volume = self.controls.current_volume_percent()
        self.playback.configure_initial_audio(initial_volume)
        self.controls.toggle_muted(self.playback.is_muted())

    def _init_timer(self):
        self.position_timer = QTimer(self)
        self.position_timer.setInterval(200)
        self.position_timer.timeout.connect(self.update_timing)
        self.position_timer.start()
        self.update_timing()
        self.playback_view_state.sync(force=True)

    def _set_position_timer_active(self, active: bool):
        if active:
            if not self.position_timer.isActive():
                self.position_timer.start()
            return
        self.position_timer.stop()

    def apply_metrics(self, metrics: Metrics):
        self.metrics = metrics
        self.controls.apply_metrics(metrics)
        self.time_popup.apply_metrics(metrics)
        self.speed_popup.apply_metrics(metrics)

        self.updateGeometry()
        self.fullscreen_controller.apply_metrics()
        self.video_placeholder.apply_metrics(metrics)
        if self.time_popup.isVisible():
            self._position_time_popup()
        if self.speed_popup.isVisible():
            self._position_speed_popup()
        self.update()

    def apply_theme(self, theme_color: ThemeState):
        self.theme_color = theme_color
        self.controls.apply_theme(theme_color)
        self.time_popup.apply_theme(theme_color)
        self.speed_popup.apply_theme(theme_color)
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self._emit_video_host_ready_if_possible()

    def resizeEvent(self, event):
        super().resizeEvent(event)

        self.fullscreen_controller.handle_resize()
        self.video_placeholder.refresh_position()

        if self.time_popup.isVisible():
            self._position_time_popup()
        if self.speed_popup.isVisible():
            self._position_speed_popup()

        self._emit_video_host_ready_if_possible()

    def eventFilter(self, watched, event):
        speed_popup = self.speed_popup if hasattr(self, "speed_popup") and isValid(self.speed_popup) else None
        if speed_popup is not None and speed_popup.isVisible():
            if event.type() == QEvent.MouseButtonPress:
                global_pos = event.globalPosition().toPoint()
                if self._click_outside_speed_popup(global_pos):
                    self._hide_speed_popup()
            elif event.type() == QEvent.MouseMove:
                self._update_speed_popup_autohide(event.globalPosition().toPoint())

        if self._is_user_activity_event(event) and hasattr(self, "video_placeholder") and isValid(self.video_placeholder):
            self.video_placeholder.notify_activity()

        if watched is self.video_frame and event.type() in {
            QEvent.Show,
            QEvent.Resize,
            QEvent.WinIdChange,
            QEvent.ShowToParent,
        }:
            self._emit_video_host_ready_if_possible()
        return super().eventFilter(watched, event)

    def _install_local_event_filters(self, widget: QWidget):
        widget.installEventFilter(self)
        for child in widget.findChildren(QWidget):
            child.installEventFilter(self)

    def set_fullscreen_mode(self, fullscreen: bool):
        self._apply_fullscreen_ui(fullscreen)

    def get_audio_tracks(self) -> list[tuple[int, str]]:
        raw_tracks = self.playback.get_audio_tracks()
        return [
            (int(track_id), self._format_track_label(track_id, track_name, "Audio"))
            for track_id, track_name in raw_tracks
        ]

    def get_audio_channel_modes(self) -> list[tuple[str, str]]:
        return [
            ("stereo", "Stereo"),
            ("mono", "Mono"),
            ("left", "Left"),
            ("right", "Right"),
            ("reverse_stereo", "Reverse Stereo"),
        ]

    def get_subtitle_tracks(self) -> list[tuple[int, str]]:
        raw_tracks = self.playback.get_subtitle_tracks()
        return [
            (int(track_id), self._format_track_label(track_id, track_name, "Subtitle"))
            for track_id, track_name in raw_tracks
        ]

    def update_timing(self):
        if self._subtitle_generation_ui_suspended:
            return
        current_ms, total_ms = self.playback.get_timing()
        self.controls.update_timing(current_ms, total_ms)

    def toggle_speed_popup(self):
        if self.speed_popup.isVisible():
            self._hide_speed_popup()
            return

        self.speed_popup.set_speed(self.playback.get_rate())
        self._position_speed_popup()
        self.speed_popup.show()
        self.speed_popup.raise_()
        self._update_speed_popup_autohide(QCursor.pos())

    def play(self):
        if not self.playback.has_media_loaded():
            return
        self.playback.play()

    def pause(self):
        if not self.playback.has_media_loaded():
            return
        self.playback.pause()

    def suspend_for_subtitle_generation(self) -> SubtitleGenerationUiSuspendLease:
        if self._subtitle_generation_ui_suspended:
            return SubtitleGenerationUiSuspendLease(self, acquired=False)
        self._apply_subtitle_generation_ui_suspend()
        return SubtitleGenerationUiSuspendLease(self, acquired=True)

    def _apply_subtitle_generation_ui_suspend(self):
        self._subtitle_generation_ui_suspended = True
        self._subtitle_generation_timer_was_active = self.position_timer.isActive()
        self.position_timer.stop()
        self.video_frame.setUpdatesEnabled(False)
        self.controls.setUpdatesEnabled(False)

    def resume_after_subtitle_generation(self):
        self._resume_subtitle_generation_ui_suspend()

    def _resume_subtitle_generation_ui_suspend(self):
        if not self._subtitle_generation_ui_suspended:
            return
        self._subtitle_generation_ui_suspended = False
        self.video_frame.setUpdatesEnabled(True)
        self.controls.setUpdatesEnabled(True)
        if self._subtitle_generation_timer_was_active:
            self.position_timer.start()
        self._subtitle_generation_timer_was_active = False
        self.update_timing()
        self.controls.update()
        self.video_frame.update()

    def on_progress_hover_changed(self, ratio: float):
        if self._pip_active:
            self.time_popup.hide()
            return
        _, total_ms = self.playback.get_timing()
        if total_ms <= 0:
            self.time_popup.hide()
            return

        hover_ms = int(max(0.0, min(1.0, ratio)) * total_ms)
        self.time_popup.set_time(hover_ms)
        self._position_time_popup()
        self.time_popup.show()
        self.time_popup.raise_()

    def on_progress_hover_left(self):
        self.time_popup.hide()

    def on_speed_changed(self, speed: float):
        applied_speed = self.player_actions.on_speed_changed(speed)
        self.controls.set_speed_value(applied_speed)

    def adjust_speed(self, delta: float):
        applied_speed = self.player_actions.adjust_speed(delta)
        self.controls.set_speed_value(applied_speed)
        if self.speed_popup.isVisible():
            self.speed_popup.set_speed(applied_speed)

    def reset_speed(self):
        applied_speed = self.player_actions.reset_speed()
        self.controls.set_speed_value(applied_speed)
        if self.speed_popup.isVisible():
            self.speed_popup.set_speed(applied_speed)

    def on_volume_changed(self, volume: int):
        desired_volume, muted = self.player_actions.on_volume_changed(volume)
        self.controls.volume_controls.volume_bar.set_volume(desired_volume / 100.0)
        self.controls.toggle_muted(muted)

    def adjust_volume(self, delta_percent: int):
        desired_volume, muted = self.player_actions.adjust_volume(delta_percent)
        self.controls.volume_controls.volume_bar.set_volume(desired_volume / 100.0)
        self.controls.toggle_muted(muted)

    def on_mute(self):
        desired_volume, muted = self.player_actions.on_mute()
        self.controls.volume_controls.volume_bar.set_volume(desired_volume / 100.0)
        self.controls.toggle_muted(muted)

    def seek_by_ms(self, delta_ms: int):
        self.player_actions.seek_by_ms(delta_ms)
        self.update_timing()

    def set_chrome_hidden(self, hidden: bool):
        self._chrome_hidden = bool(hidden)
        self.fullscreen_controller.set_controls_forced_hidden(self._chrome_hidden)
        if self.speed_popup.isVisible() and self._chrome_hidden:
            self._hide_speed_popup()
        self.updateGeometry()
        self.update()

    def is_chrome_hidden(self) -> bool:
        return self._chrome_hidden

    def _is_user_activity_event(self, event) -> bool:
        return event.type() in {
            QEvent.MouseMove,
            QEvent.MouseButtonPress,
            QEvent.MouseButtonDblClick,
            QEvent.Wheel,
            QEvent.KeyPress,
        }

    def _format_track_label(self, track_id, track_name, prefix: str) -> str:
        if isinstance(track_name, bytes):
            track_name = track_name.decode("utf-8", errors="replace")

        label = str(track_name).strip() if track_name is not None else ""
        if label:
            return label
        if int(track_id) == -1:
            return "Disable"
        return f"{prefix} {track_id}"

    def _position_time_popup(self):
        popup_w, popup_h = self.time_popup.preferred_size()

        cursor_global = QCursor.pos()
        min_x = self.mapToGlobal(QPoint(0, 0)).x()
        controls_top_global = self.controls.mapToGlobal(QPoint(0, 0)).y()

        x = int(cursor_global.x() - popup_w / 2.0)
        y = int(controls_top_global - popup_h)

        max_x = min_x + self.width() - popup_w
        x = max(min_x, min(max_x, x))

        self.time_popup.setGeometry(x, y, popup_w, popup_h)

    def _click_outside_speed_popup(self, global_pos: QPoint) -> bool:
        popup_rect = self.speed_popup.geometry()
        button_top_left = self.controls.speed_button.mapToGlobal(QPoint(0, 0))
        button_rect = self.controls.speed_button.rect().translated(button_top_left)
        return not popup_rect.contains(global_pos) and not button_rect.contains(global_pos)

    def _hide_speed_popup(self):
        self.speed_popup_autohide_timer.stop()
        self.speed_popup.hide()

    def _is_cursor_in_speed_popup(self, global_pos: QPoint) -> bool:
        return self.speed_popup.geometry().contains(global_pos)

    def _update_speed_popup_autohide(self, global_pos: QPoint):
        if self._is_cursor_in_speed_popup(global_pos):
            self.speed_popup_autohide_timer.stop()
            return
        self.speed_popup_autohide_timer.start()

    def _on_speed_popup_autohide_timeout(self):
        if not self.speed_popup.isVisible():
            return
        if self._is_cursor_in_speed_popup(QCursor.pos()):
            return
        self._hide_speed_popup()

    def _position_speed_popup(self):
        popup_w, popup_h = self.speed_popup.preferred_size()

        button_top_left = self.controls.speed_button.mapToGlobal(QPoint(0, 0))
        button_center_x = button_top_left.x() + self.controls.speed_button.width() / 2.0
        min_x = self.mapToGlobal(QPoint(0, 0)).x()
        controls_top_global = self.controls.mapToGlobal(QPoint(0, 0)).y()

        x = int(button_center_x - popup_w / 4.0)
        y = int(controls_top_global - popup_h) - 2

        max_x = min_x + self.width() - popup_w
        x = max(min_x, min(max_x, x))

        self.speed_popup.setGeometry(x, y, popup_w, popup_h)

    def is_pip_active(self) -> bool:
        return self._pip_active

    def set_pip_active(self, active: bool):
        self._pip_active = bool(active)
        self._apply_pip_ui(self._pip_active)

    def bind_video_output(self):
        self.video_frame.winId()
        self.playback.bind_video_output(int(self.video_frame.winId()))

    def is_video_host_ready(self) -> bool:
        if not self.isVisible() or not self.video_frame.isVisible():
            return False
        if self.video_frame.width() <= 0 or self.video_frame.height() <= 0:
            return False
        try:
            return int(self.video_frame.winId()) != 0
        except RuntimeError:
            return False

    def _on_current_media_changed(self, path: str):
        self.current_media_changed.emit(path)

    def _apply_playback_view_state(self, view_state: PlaybackViewState):
        self._set_position_timer_active(view_state.position_timer_active)
        if view_state.placeholder_visible:
            self.video_placeholder.show_placeholder()
        else:
            self.video_placeholder.hide_placeholder()
        self.controls.toggle_progress_seekable(view_state.progress_seekable)
        self.controls.toggle_play_pause(view_state.play_pause_shows_playing)
        if not view_state.position_timer_active:
            self.update_timing()

    def _apply_fullscreen_ui(self, fullscreen: bool):
        self.fullscreen_controller.set_fullscreen_mode(fullscreen)
        self.controls.toggle_fullscreen(fullscreen)

    def _apply_pip_ui(self, active: bool):
        self.fullscreen_controller.set_pip_mode(active)
        self.controls.set_pip_mode(active)
        if active:
            self.time_popup.hide()
        if self.speed_popup.isVisible() and active:
            self._hide_speed_popup()
        self.updateGeometry()
        self.update()

    def _emit_video_host_ready_if_possible(self):
        if self.is_video_host_ready():
            self.video_host_ready.emit()
