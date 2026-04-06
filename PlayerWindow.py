from PySide6.QtCore import Qt, QTimer, Signal, QPoint
from PySide6.QtGui import QPalette, QColor, QCursor
from PySide6.QtWidgets import QWidget
from PySide6.QtSvgWidgets import QSvgWidget

from PlaybackEngine import PlaybackEngine
from PlayerFullscreenController import PlayerFullscreenController
from PlaybackPlaylist import PlaybackPlaylist
from PlayerControls import PlayerControls, TimePopup
from utils import Metrics, res_path
from ThemeColor import ThemeColor


class PlayerWindow(QWidget):
    open_file_requested = Signal()
    media_finished = Signal(str)
    fullscreen_requested = Signal()

    # ──────────────────────────── initialization ────────────────────────────

    def __init__(self, metrics: Metrics | None = None, theme_color: ThemeColor | None = None):
        super().__init__()
        self.setAttribute(Qt.WA_StyledBackground, True)

        self.metrics = metrics
        self.theme_color = theme_color

        self.engine = PlaybackEngine(self)
        self._init_state()
        self._init_video_frame()
        self._init_controls()
        self._init_audio()
        self._init_timer()

    def _init_state(self):
        self._video_bound = False
        self._resume_after_seek = False

        self.playback_playlist = PlaybackPlaylist()
        
        self._exit_after_current = False

    def _init_video_frame(self):
        self.video_frame = QWidget(self)
        self.video_frame.setMouseTracking(True)
        self.video_frame.setAutoFillBackground(True)
        palette = self.video_frame.palette()
        palette.setColor(QPalette.Window, QColor(0, 0, 0))
        self.video_frame.setPalette(palette)

        self.video_placeholder = QSvgWidget(res_path("assets/logo.svg"), self.video_frame)
        self.video_placeholder.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.video_placeholder.show()

    def _init_controls(self):  
        self.controls = PlayerControls(self, self.metrics, self.theme_color)  
        
        self.controls.play_pause_button.clicked.connect(self.on_play_pause)  
        self.controls.stop_button.clicked.connect(self.on_stop)  
        self.controls.progress_bar.seek_started.connect(self.on_seek_started)
        self.controls.progress_bar.value_changed.connect(self.on_seek)
        self.controls.progress_bar.seek_finished.connect(self.on_seek_finished)
        self.controls.progress_bar.hover_changed.connect(self.on_progress_hover_changed)
        self.controls.progress_bar.hover_left.connect(self.on_progress_hover_left)
        self.controls.volume_controls.volume_bar.volume_changed.connect( 
            lambda v: self.on_volume_changed(int(v * 100)) 
        )
        self.controls.volume_controls.volume_button.clicked.connect(self.on_mute)  
        self.controls.fullscreen_button.clicked.connect(self.on_fullscreen)  
        self.controls.pip_button.clicked.connect(self.on_pip)
        self.controls.rewind_lbutton.clicked.connect(self.on_prev)
        self.controls.rewind_rbutton.clicked.connect(self.on_next)
        self.controls.rewind_lbutton.seek_hold.connect(self.on_seek_hold)
        self.controls.rewind_rbutton.seek_hold.connect(self.on_seek_hold)

        self.time_popup = TimePopup(None, metrics=self.metrics, theme_color=self.theme_color)
        self.time_popup.hide()
        self.fullscreen_controller = PlayerFullscreenController(
            self,
            self.video_frame,
            self.controls,
            self.time_popup,
            has_media_loaded=self.has_media_loaded,
            toggle_play_pause=self.on_play_pause,
            request_fullscreen=self.on_fullscreen,
        )
        self.fullscreen_controller.apply_metrics(self.metrics)

    def _init_audio(self):
        initial_volume = self.controls.current_volume_percent()
        self.engine.set_volume(initial_volume)
        self.engine.set_last_volume_before_mute(initial_volume)
        self.controls.toggle_muted(self.engine.is_muted())

    def _init_timer(self):
        self.position_timer = QTimer(self)
        self.position_timer.setInterval(200)
        self.position_timer.timeout.connect(self.update_timing)
        self.position_timer.start()
        self.update_timing()
        self.engine.media_ended.connect(self._handle_media_end)

    # ────────────────────────── Qt events ────────────────────────────────

    def apply_metrics(self, metrics: Metrics):
        self.metrics = metrics
        self.controls.apply_metrics(metrics)
        self.time_popup.apply_metrics(metrics)

        self.updateGeometry()
        self.fullscreen_controller.apply_metrics(metrics)
        self._position_video_placeholder()
        if self.time_popup.isVisible():
            self._position_time_popup()
        self.update()

    def apply_theme(self, theme_color: ThemeColor):
        self.theme_color = theme_color
        self.controls.apply_theme(theme_color)
        self.time_popup.apply_theme(theme_color)
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        
        if not self._video_bound:
            self.engine.bind_video_output(int(self.video_frame.winId()))
            self._video_bound = True

    def resizeEvent(self, event):
        super().resizeEvent(event)

        self.fullscreen_controller.handle_resize()
        self._position_video_placeholder()

        if self.time_popup.isVisible():
            self._position_time_popup()

    # ─────────────────────────── public methods ──────────────────────────

    def load_playlist(self, file_paths: list[str], start_index: int = 0) -> bool:
        if not self.playback_playlist.load(file_paths, start_index=start_index):
            return False
        return self._load_current_media()

    def open_paths(self, file_paths: list[str], start_index: int = 0, start_position_ms: int = 0) -> bool:
        if not self.load_playlist(file_paths, start_index=start_index):
            return False
        self.play_loaded_media(start_position_ms=start_position_ms)
        return True

    def current_media_path(self) -> str | None:
        return self.playback_playlist.current_path()

    def get_session_snapshot(self) -> dict[str, int | str] | None:
        current_path = self.current_media_path()
        if current_path is None:
            return None

        return {
            "path": current_path,
            "position_ms": self.engine.get_time(),
            "total_ms": self.engine.get_length(),
        }

    def play_loaded_media(self, start_position_ms: int = 0):
        self.video_placeholder.hide()
        self.engine.sync_audio_to_player()
        self.engine.play()
        if start_position_ms > 0:
            QTimer.singleShot(0, lambda: self.engine.set_time(start_position_ms))
        self.controls.toggle_play_pause(True)

    def set_exit_after_current(self, enabled: bool):
        self._exit_after_current = bool(enabled)

    def is_exit_after_current_enabled(self) -> bool:
        return self._exit_after_current

    def has_media_loaded(self) -> bool:
        return self.engine.get_media() is not None

    def set_fullscreen_mode(self, fullscreen: bool):
        self.fullscreen_controller.set_fullscreen_mode(fullscreen)

    def get_audio_tracks(self) -> list[tuple[int, str]]:
        raw_tracks = self.engine.get_audio_tracks()
        return [
            (int(track_id), self._format_track_label(track_id, track_name, "Audio"))
            for track_id, track_name in raw_tracks
        ]

    def get_current_audio_track(self) -> int:
        return self.engine.get_current_audio_track()

    def set_audio_track(self, track_id: int) -> bool:
        return self.engine.set_audio_track(track_id)

    def get_audio_devices(self) -> list[tuple[str, str]]:
        return self.engine.get_audio_devices()

    def get_current_audio_device(self) -> str:
        return self.engine.get_current_audio_device()

    def set_audio_device(self, device_id: str) -> bool:
        return self.engine.set_audio_device(device_id)

    def get_audio_channel_modes(self) -> list[tuple[str, str]]:
        return [
            ("stereo", "Stereo"),
            ("mono", "Mono"),
            ("left", "Left"),
            ("right", "Right"),
            ("reverse_stereo", "Reverse Stereo"),
        ]

    def get_current_audio_channel(self) -> str:
        return self.engine.get_current_audio_mode()

    def set_audio_channel(self, channel: str) -> bool:
        return self.engine.set_audio_mode(channel)

    def get_subtitle_tracks(self) -> list[tuple[int, str]]:
        raw_tracks = self.engine.get_subtitle_tracks()
        return [
            (int(track_id), self._format_track_label(track_id, track_name, "Subtitle"))
            for track_id, track_name in raw_tracks
        ]

    def get_current_subtitle_track(self) -> int:
        return self.engine.get_current_subtitle_track()

    def set_subtitle_track(self, track_id: int) -> bool:
        return self.engine.set_subtitle_track(track_id)

    def open_subtitle_file(self, subtitle_path: str) -> bool:
        return self.engine.open_subtitle_file(subtitle_path)

    def update_timing(self):  
        current_ms = self.engine.get_time()
        total_ms = self.engine.get_length()
        self.controls.update_timing(current_ms, total_ms)

    # ─────────────────────── playback control slots ────────────

    def on_play_pause(self):  
        if self.engine.get_media() is None:
            self.open_file_requested.emit()
            return

        self.controls.toggle_progress_seekable(True)
        if self.engine.is_playing():
            self.engine.pause()
            self.controls.toggle_play_pause(False)  
        else:  
            self.engine.sync_audio_to_player()
            self.engine.play()
            self.controls.toggle_play_pause(True)  

    def on_stop(self):  
        self.engine.stop()
        self._apply_stop_state()

    def on_fullscreen(self):
        if not self.fullscreen_controller.is_fullscreen() and not self.has_media_loaded():
            return
        self.fullscreen_requested.emit()

    def on_pip(self):
        pass  # TODO: реализовать режим «картинка в картинке» (PiP)

    def on_prev(self):
        if not self.playback_playlist.move_previous_wrap():
            return
        if self._load_current_media():
            self.play_loaded_media()

    def on_next(self):
        if not self.playback_playlist.move_next_wrap():
            return
        if self._load_current_media():
            self.play_loaded_media()

    def on_seek_hold(self, direction: str):
        current_ms = self.engine.get_time()
        if current_ms < 0:
            return
        step_ms = -10_000 if direction == "left" else 10_000
        new_ms = max(0, current_ms + step_ms)
        total_ms = self.engine.get_length()
        if total_ms > 0:
            self.engine.set_position(new_ms / total_ms)

    # ─────────────────────── seek slots ──────────────────────────────

    def on_seek_started(self):
        self._resume_after_seek = self.engine.is_playing()
        if self._resume_after_seek:  
            self.engine.pause()

    def on_seek(self, value: float):  
        if self.engine.is_seekable():
            self.engine.set_position(max(0.0, min(1.0, value)))

    def on_seek_finished(self):  
        if self._resume_after_seek:  
            self.engine.play()
        self._resume_after_seek = False  

    def on_progress_hover_changed(self, ratio: float):
        total_ms = self.engine.get_length()
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

    # ─────────────────────── volume and mute slots ───────────────────────

    def on_volume_changed(self, volume: int): 
        desired_volume = max(0, min(100, volume))
        self.engine.set_volume(desired_volume)
        if desired_volume > 0:
            self.engine.set_last_volume_before_mute(desired_volume)
            if self.engine.is_muted():
                self.engine.set_muted(False)
                self.controls.toggle_muted(False)
        self.engine.sync_audio_to_player()

    def on_mute(self):  
        if not self.engine.is_muted():
            desired_volume = self.engine.get_desired_volume()
            if desired_volume > 0:
                self.engine.set_last_volume_before_mute(desired_volume)
            self.engine.set_volume(0)
            self.engine.set_muted(True)
        else:
            self.engine.set_muted(False)
            self.engine.set_volume(max(1, self.engine.get_last_volume_before_mute()))
        self.controls.volume_controls.volume_bar.set_volume(self.engine.get_desired_volume() / 100.0)
        self.engine.sync_audio_to_player()
        self.controls.toggle_muted(self.engine.is_muted())

    # ─────────────────────── private helper methods ─────────────

    def _handle_media_end(self):
        finished_path = self.current_media_path()
        if finished_path:
            self.media_finished.emit(finished_path)

        if self._exit_after_current:
            self.engine.stop()
            self._apply_stop_state()

            from PySide6.QtWidgets import QApplication
            QApplication.instance().quit()
            return

        if self._play_next_from_playlist():
            return

        self.engine.stop()
        self._apply_stop_state()

    def _load_current_media(self) -> bool:
        media_path = self.playback_playlist.current_path()
        if media_path is None:
            return False
        self.engine.load_media(media_path)
        self.video_placeholder.hide()
        self.controls.toggle_progress_seekable(True)
        return True

    def _play_next_from_playlist(self) -> bool:
        if not self.playback_playlist.move_next_linear():
            return False
        if self._load_current_media():
            self.play_loaded_media()
            return True
        return False

    def _apply_stop_state(self):
        self.video_placeholder.show()
        self._position_video_placeholder()
        self.controls.toggle_play_pause(False)
        self.controls.toggle_progress_seekable(False)
        current_ms = self.engine.get_time()
        total_ms = self.engine.get_length()
        self.controls.update_timing(current_ms, total_ms)

    def _position_video_placeholder(self):
        frame_width = self.video_frame.width()
        frame_height = self.video_frame.height()
        logo_size = max(96, min(frame_width, frame_height) // 5)
        x = max(0, (frame_width - logo_size) // 2)
        y = max(0, (frame_height - logo_size) // 2)
        self.video_placeholder.setGeometry(x, y, logo_size, logo_size)

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
