from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, Signal

from services.PlaybackEngine import PlaybackService
from models.PlaybackPlaylist import PlaylistState


class PlayerPlaybackController(QObject):
    STATE_STOPPED = "stopped"
    STATE_PAUSED = "paused"
    STATE_PLAYING = "playing"

    playback_state_changed = Signal(str)
    media_finished = Signal(str)
    current_media_changed = Signal(str)
    video_geometry_changed = Signal(int, int)
    exit_after_current_requested = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.engine = PlaybackService(self)
        self.playlist = PlaylistState()

        self._resume_after_seek = False
        self._exit_after_current = False
        self._playback_state = self.STATE_STOPPED

        self.engine.media_ended.connect(self._handle_media_end)
        self.engine.video_geometry_changed.connect(self.video_geometry_changed)

    def current_media_path(self) -> str | None:
        return self.playlist.current_path()

    def get_session_snapshot(self) -> dict[str, int | str] | None:
        current_path = self.current_media_path()
        if current_path is None:
            return None

        return {
            "path": current_path,
            "position_ms": self.engine.get_time(),
            "total_ms": self.engine.get_length(),
        }

    def load_playlist(self, file_paths: list[str], start_index: int = 0) -> bool:
        if not self.playlist.load(file_paths, start_index=start_index):
            return False
        return self._load_current_media()

    def open_paths(self, file_paths: list[str], start_index: int = 0, start_position_ms: int = 0) -> bool:
        if not self.load_playlist(file_paths, start_index=start_index):
            return False
        self.play_loaded_media(start_position_ms=start_position_ms)
        return True

    def play_loaded_media(self, start_position_ms: int = 0):
        self.engine.sync_audio_to_player()
        self.engine.play()
        if start_position_ms > 0:
            QTimer.singleShot(0, lambda: self.engine.set_time(start_position_ms))
        self._set_playback_state(self.STATE_PLAYING)

    def toggle_play_pause(self):
        if self.engine.is_playing():
            self.engine.pause()
            self._set_playback_state(self.STATE_PAUSED)
            return

        self.engine.sync_audio_to_player()
        self.engine.play()
        self._set_playback_state(self.STATE_PLAYING)

    def stop(self):
        self.engine.stop()
        self._set_playback_state(self.STATE_STOPPED)

    def play_previous(self) -> bool:
        if not self.playlist.move_previous_wrap():
            return False
        if not self._load_current_media():
            return False
        self.play_loaded_media()
        return True

    def play_next(self) -> bool:
        if not self.playlist.move_next_wrap():
            return False
        if not self._load_current_media():
            return False
        self.play_loaded_media()
        return True

    def begin_seek(self):
        self._resume_after_seek = self.engine.is_playing()
        if self._resume_after_seek:
            self.engine.pause()

    def seek_to_ratio(self, value: float):
        if self.engine.is_seekable():
            self.engine.set_position(max(0.0, min(1.0, value)))

    def finish_seek(self):
        if self._resume_after_seek:
            self.engine.play()
            self._set_playback_state(self.STATE_PLAYING)
        self._resume_after_seek = False

    def seek_by_hold(self, direction: str):
        current_ms = self.engine.get_time()
        if current_ms < 0:
            return

        step_ms = -10_000 if direction == "left" else 10_000
        new_ms = max(0, current_ms + step_ms)
        total_ms = self.engine.get_length()
        if total_ms > 0:
            self.engine.set_position(new_ms / total_ms)

    def set_exit_after_current(self, enabled: bool):
        self._exit_after_current = bool(enabled)

    def is_exit_after_current_enabled(self) -> bool:
        return self._exit_after_current

    def has_media_loaded(self) -> bool:
        return self.engine.get_media() is not None

    def can_activate_view_modes(self) -> bool:
        return self.has_media_loaded() and self._playback_state != self.STATE_STOPPED

    def get_timing(self) -> tuple[int, int]:
        return self.engine.get_time(), self.engine.get_length()

    def get_rate(self) -> float:
        return self.engine.get_rate()

    def set_rate(self, speed: float):
        self.engine.set_rate(speed)

    def get_audio_tracks(self):
        return self.engine.get_audio_tracks()

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

    def get_current_audio_mode(self) -> str:
        return self.engine.get_current_audio_mode()

    def set_audio_mode(self, channel: str) -> bool:
        return self.engine.set_audio_mode(channel)

    def get_subtitle_tracks(self):
        return self.engine.get_subtitle_tracks()

    def get_current_subtitle_track(self) -> int:
        return self.engine.get_current_subtitle_track()

    def set_subtitle_track(self, track_id: int) -> bool:
        return self.engine.set_subtitle_track(track_id)

    def open_subtitle_file(self, subtitle_path: str) -> bool:
        return self.engine.open_subtitle_file(subtitle_path)

    def get_desired_volume(self) -> int:
        return self.engine.get_desired_volume()

    def is_muted(self) -> bool:
        return self.engine.is_muted()

    def configure_initial_audio(self, volume: int):
        self.engine.set_volume(volume)
        self.engine.set_last_volume_before_mute(volume)

    def set_volume(self, volume: int):
        desired_volume = max(0, min(100, volume))
        self.engine.set_volume(desired_volume)
        if desired_volume > 0:
            self.engine.set_last_volume_before_mute(desired_volume)
            if self.engine.is_muted():
                self.engine.set_muted(False)
        self.engine.sync_audio_to_player()

    def toggle_mute(self):
        if not self.engine.is_muted():
            desired_volume = self.engine.get_desired_volume()
            if desired_volume > 0:
                self.engine.set_last_volume_before_mute(desired_volume)
            self.engine.set_volume(0)
            self.engine.set_muted(True)
        else:
            self.engine.set_muted(False)
            self.engine.set_volume(max(1, self.engine.get_last_volume_before_mute()))

        self.engine.sync_audio_to_player()

    def bind_video_output(self, win_id: int):
        self.engine.bind_video_output(win_id)

    def get_video_dimensions(self) -> tuple[int, int] | None:
        return self.engine.get_video_dimensions()

    def _handle_media_end(self):
        finished_path = self.current_media_path()
        if finished_path:
            self.media_finished.emit(finished_path)

        if self._exit_after_current:
            self.stop()
            self.exit_after_current_requested.emit()
            return

        if self._play_next_from_playlist():
            return

        self.stop()

    def _play_next_from_playlist(self) -> bool:
        if not self.playlist.move_next_linear():
            return False
        if not self._load_current_media():
            return False
        self.play_loaded_media()
        return True

    def _load_current_media(self) -> bool:
        media_path = self.playlist.current_path()
        if media_path is None:
            return False

        self.engine.load_media(media_path)
        self.current_media_changed.emit(media_path)
        return True

    def _set_playback_state(self, state: str):
        if self._playback_state == state:
            return
        self._playback_state = state
        self.playback_state_changed.emit(state)
