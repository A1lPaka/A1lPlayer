import os

import vlc

from PySide6.QtCore import QObject, QTimer, Signal


class PlaybackEngine(QObject):
    playing = Signal()
    media_ended = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._desired_volume = 100
        self._desired_muted = False
        self._last_volume_before_mute = self._desired_volume
        self._desired_audio_mode = "stereo"
        self._current_media_path: str | None = None
        self._bound_win_id: int | None = None
        self._audio_modes = {
            "stereo": {
                "stereo_mode": 1,
                "channel": int(vlc.AudioOutputChannel.Stereo.value),
            },
            "reverse_stereo": {
                "stereo_mode": 2,
                "channel": int(vlc.AudioOutputChannel.RStereo.value),
            },
            "left": {
                "stereo_mode": 3,
                "channel": int(vlc.AudioOutputChannel.Left.value),
            },
            "right": {
                "stereo_mode": 4,
                "channel": int(vlc.AudioOutputChannel.Right.value),
            },
        }
        self._create_backend()

        self.playing.connect(self.sync_audio_to_player)

    def _create_backend(self):
        self.instance = vlc.Instance(
            f"--stereo-mode={self._audio_modes[self._desired_audio_mode]['stereo_mode']}"
        )
        self.player = self.instance.media_player_new()

        event_manager = self.player.event_manager()
        event_manager.event_attach(vlc.EventType.MediaPlayerPlaying, self._on_vlc_playing_event)
        event_manager.event_attach(vlc.EventType.MediaPlayerEndReached, self._on_vlc_media_ended_event)

        if self._bound_win_id is not None:
            self.bind_video_output(self._bound_win_id)

    def _get_runtime_audio_channel(self, mode: str) -> int | None:
        return self._audio_modes[mode]["channel"]

    def bind_video_output(self, win_id: int):
        self._bound_win_id = win_id
        if os.name == "nt":
            self.player.set_hwnd(win_id)
        elif os.name == "posix":
            self.player.set_xwindow(win_id)

    def load_media(self, media_path: str):
        self._current_media_path = media_path
        media = self.instance.media_new(media_path)
        self.player.set_media(media)

    def get_media(self):
        return self.player.get_media()

    def get_state(self):
        return self.player.get_state()

    def is_playing(self) -> bool:
        return self.player.get_state() == vlc.State.Playing

    def is_seekable(self) -> bool:
        return self.player.is_seekable()

    def play(self):
        self.player.play()

    def pause(self):
        self.player.pause()

    def stop(self):
        self.player.stop()

    def set_time(self, position_ms: int):
        self.player.set_time(position_ms)

    def get_time(self) -> int:
        return int(self.player.get_time())

    def get_length(self) -> int:
        return int(self.player.get_length())

    def set_position(self, position: float):
        self.player.set_position(position)

    def get_audio_tracks(self):
        return self.player.audio_get_track_description() or []

    def get_current_audio_track(self) -> int:
        return int(self.player.audio_get_track())

    def set_audio_track(self, track_id: int) -> bool:
        return self.player.audio_set_track(int(track_id)) == 0

    def get_current_audio_mode(self) -> str:
        return self._desired_audio_mode

    def set_audio_mode(self, mode: str) -> bool:
        mode = str(mode)
        if mode not in self._audio_modes:
            return False

        if mode == self._desired_audio_mode:
            return True

        self._desired_audio_mode = mode

        runtime_channel = self._get_runtime_audio_channel(mode)
        if runtime_channel is not None:
            return self.player.audio_set_channel(runtime_channel) == 0

        return True

    def get_subtitle_tracks(self):
        return self.player.video_get_spu_description() or []

    def get_current_subtitle_track(self) -> int:
        return int(self.player.video_get_spu())

    def set_subtitle_track(self, track_id: int) -> bool:
        return self.player.video_set_spu(int(track_id)) == 0

    def set_volume(self, volume: int):
        self._desired_volume = max(0, min(100, volume))
        self.player.audio_set_volume(self._desired_volume)

    def get_desired_volume(self) -> int:
        return self._desired_volume

    def set_muted(self, muted: bool):
        self._desired_muted = bool(muted)
        self.player.audio_set_mute(self._desired_muted)

    def is_muted(self) -> bool:
        return self._desired_muted

    def get_last_volume_before_mute(self) -> int:
        return self._last_volume_before_mute

    def set_last_volume_before_mute(self, volume: int):
        self._last_volume_before_mute = max(0, min(100, volume))

    def sync_audio_to_player(self):
        self.player.audio_set_volume(self._desired_volume)
        self.player.audio_set_mute(self._desired_muted)
        desired_channel = self._get_runtime_audio_channel(self._desired_audio_mode)
        if desired_channel is not None:
            self.player.audio_set_channel(desired_channel)

        QTimer.singleShot(150, lambda: self.player.audio_set_volume(self._desired_volume))
        QTimer.singleShot(150, lambda: self.player.audio_set_mute(self._desired_muted))
        if desired_channel is not None:
            QTimer.singleShot(150, lambda: self.player.audio_set_channel(desired_channel))

    def _on_vlc_playing_event(self, event):
        self.playing.emit()

    def _on_vlc_media_ended_event(self, event):
        self.media_ended.emit()
