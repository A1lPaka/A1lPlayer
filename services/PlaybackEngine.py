import os
import shutil
import tempfile
import uuid
from pathlib import Path

import vlc

from PySide6.QtCore import QObject, QMetaObject, Qt, QTimer, Signal, Slot

VLC_AUDIO_CHANNEL_MONO = 7
AUDIO_DEVICE_DEFAULT_ID = "__default__"


class PlaybackService(QObject):
    playing = Signal()
    paused = Signal()
    stopped = Signal()
    media_ended = Signal()
    video_geometry_changed = Signal(int, int)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._desired_volume = 100
        self._desired_muted = False
        self._desired_rate = 1.0
        self._last_volume_before_mute = self._desired_volume
        self._desired_audio_mode = "stereo"
        self._desired_audio_device_id: str | None = None
        self._current_media_path: str | None = None
        self._bound_win_id: int | None = None
        self._last_video_geometry: tuple[int, int] | None = None
        self._runtime_subtitle_copy_path: str | None = None
        self._audio_modes = {
            "stereo": {
                "channel": int(vlc.AudioOutputChannel.Stereo.value),
            },
            "reverse_stereo": {
                "channel": int(vlc.AudioOutputChannel.RStereo.value),
            },
            "left": {
                "channel": int(vlc.AudioOutputChannel.Left.value),
            },
            "right": {
                "channel": int(vlc.AudioOutputChannel.Right.value),
            },
            "mono": {
                # python-vlc does not expose Mono in AudioOutputChannel,
                # but libVLC accepts the raw channel id 7 for mono mode.
                "channel": VLC_AUDIO_CHANNEL_MONO,
            },
        }
        self._create_backend()

        self.playing.connect(self.sync_audio_to_player)

    def _create_backend(self):
        self.instance = vlc.Instance()
        self.player = self.instance.media_player_new()
        self._attach_event_handlers()

        if self._bound_win_id is not None:
            self.bind_video_output(self._bound_win_id)

    def _attach_event_handlers(self):
        event_manager = self.player.event_manager()
        event_manager.event_attach(vlc.EventType.MediaPlayerPlaying, self._on_vlc_playing_event)
        event_manager.event_attach(vlc.EventType.MediaPlayerPaused, self._on_vlc_paused_event)
        event_manager.event_attach(vlc.EventType.MediaPlayerStopped, self._on_vlc_stopped_event)
        event_manager.event_attach(vlc.EventType.MediaPlayerEndReached, self._on_vlc_media_ended_event)

    def _get_runtime_audio_channel(self, mode: str) -> int | None:
        return self._audio_modes[mode]["channel"]

    def _decode_vlc_text(self, value) -> str | None:
        if value is None:
            return None
        if isinstance(value, bytes):
            return value.decode(errors="ignore")
        return str(value)

    def _iter_vlc_linked_list(self, head):
        node = head
        while node:
            item = node.contents
            yield item
            node = item.next

    def bind_video_output(self, win_id: int):
        self._bound_win_id = win_id
        if os.name == "nt":
            self.player.set_hwnd(win_id)
        elif os.name == "posix":
            self.player.set_xwindow(win_id)
        self._disable_vout_input()

    def load_media(self, media_path: str):
        self._cleanup_runtime_subtitle_copy()
        self._current_media_path = media_path
        self._last_video_geometry = None
        media = self.instance.media_new(media_path)
        self.player.set_media(media)

    def get_media(self):
        return self.player.get_media()

    def is_playing(self) -> bool:
        return self.player.get_state() == vlc.State.Playing

    def is_seekable(self) -> bool:
        return self.player.is_seekable()

    def play(self):
        self.player.play()

    def pause(self):
        self.player.pause()

    def stop(self):
        self._cleanup_runtime_subtitle_copy()
        self.player.stop()

    def set_time(self, position_ms: int):
        self.player.set_time(position_ms)

    def get_time(self) -> int:
        return int(self.player.get_time())

    def get_length(self) -> int:
        return int(self.player.get_length())

    def get_video_dimensions(self) -> tuple[int, int] | None:
        try:
            size = self.player.video_get_size(0)
        except Exception:
            return None

        if not size or len(size) < 2:
            return None

        width = int(size[0] or 0)
        height = int(size[1] or 0)
        if width <= 0 or height <= 0:
            return None
        return width, height

    def set_position(self, position: float):
        self.player.set_position(position)

    def set_rate(self, rate: float) -> bool:
        clamped_rate = max(0.25, min(4.0, float(rate)))
        self._desired_rate = clamped_rate
        return self.player.set_rate(clamped_rate) == 0

    def get_rate(self) -> float:
        return self._desired_rate

    def get_audio_tracks(self):
        return self.player.audio_get_track_description() or []

    def get_current_audio_track(self) -> int:
        return int(self.player.audio_get_track())

    def set_audio_track(self, track_id: int) -> bool:
        return self.player.audio_set_track(int(track_id)) == 0

    def get_audio_devices(self) -> list[tuple[str, str]]:
        devices: list[tuple[str, str]] = []
        seen_device_ids: set[str] = set()

        for device_item in self._iter_vlc_linked_list(self.player.audio_output_device_enum()):
            raw_device_id = self._decode_vlc_text(device_item.device)
            device_title = self._decode_vlc_text(device_item.description)
            normalized_device_id = raw_device_id or AUDIO_DEVICE_DEFAULT_ID
            normalized_title = device_title or "Default Device"

            if normalized_device_id in seen_device_ids:
                continue

            devices.append((normalized_device_id, normalized_title))
            seen_device_ids.add(normalized_device_id)

        if AUDIO_DEVICE_DEFAULT_ID not in seen_device_ids:
            devices.insert(0, (AUDIO_DEVICE_DEFAULT_ID, "Default Device"))

        return devices

    def get_current_audio_device(self) -> str:
        current_device_id = self._decode_vlc_text(self.player.audio_output_device_get())
        return current_device_id or AUDIO_DEVICE_DEFAULT_ID

    def set_audio_device(self, device_id: str) -> bool:
        normalized_device_id = None if device_id == AUDIO_DEVICE_DEFAULT_ID else str(device_id)
        self._desired_audio_device_id = normalized_device_id
        self.player.audio_output_device_set(None, normalized_device_id)
        return True

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

    def open_subtitle_file(self, subtitle_path: str) -> bool:
        if not subtitle_path or self.get_media() is None:
            return False

        runtime_path = self._prepare_runtime_subtitle_copy(subtitle_path)
        if runtime_path is None:
            return False

        self.player.video_set_spu(-1)
        if self.player.video_set_subtitle_file(runtime_path) != 0:
            self._remove_subtitle_copy(runtime_path)
            return False

        self._runtime_subtitle_copy_path = runtime_path
        return True

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
        self.player.set_rate(self._desired_rate)
        self.player.audio_set_volume(self._desired_volume)
        self.player.audio_set_mute(self._desired_muted)
        self.player.audio_output_device_set(None, self._desired_audio_device_id)
        desired_channel = self._get_runtime_audio_channel(self._desired_audio_mode)
        if desired_channel is not None:
            self.player.audio_set_channel(desired_channel)

        QTimer.singleShot(150, lambda: self.player.set_rate(self._desired_rate))
        QTimer.singleShot(150, lambda: self.player.audio_set_volume(self._desired_volume))
        QTimer.singleShot(150, lambda: self.player.audio_set_mute(self._desired_muted))
        QTimer.singleShot(150, lambda: self.player.audio_output_device_set(None, self._desired_audio_device_id))
        if desired_channel is not None:
            QTimer.singleShot(150, lambda: self.player.audio_set_channel(desired_channel))

    def _on_vlc_playing_event(self, event):
        QMetaObject.invokeMethod(self, "_emit_playing_from_qt_thread", Qt.QueuedConnection)

    def _on_vlc_paused_event(self, event):
        QMetaObject.invokeMethod(self, "_emit_paused_from_qt_thread", Qt.QueuedConnection)

    def _on_vlc_stopped_event(self, event):
        QMetaObject.invokeMethod(self, "_emit_stopped_from_qt_thread", Qt.QueuedConnection)

    def _on_vlc_media_ended_event(self, event):
        QMetaObject.invokeMethod(self, "_emit_media_ended_from_qt_thread", Qt.QueuedConnection)

    @Slot()
    def _emit_playing_from_qt_thread(self):
        self.playing.emit()
        self._schedule_video_geometry_probe()

    @Slot()
    def _emit_paused_from_qt_thread(self):
        self.paused.emit()

    @Slot()
    def _emit_stopped_from_qt_thread(self):
        self.stopped.emit()

    @Slot()
    def _emit_media_ended_from_qt_thread(self):
        self.media_ended.emit()

    def _disable_vout_input(self):
        try:
            self.player.video_set_mouse_input(False)
        except Exception:
            pass

        try:
            self.player.video_set_key_input(False)
        except Exception:
            pass

    def _schedule_video_geometry_probe(self, attempts: int = 12, delay_ms: int = 120):
        if attempts <= 0:
            return

        geometry = self.get_video_dimensions()
        if geometry is not None:
            if geometry != self._last_video_geometry:
                self._last_video_geometry = geometry
                self.video_geometry_changed.emit(*geometry)
            return

        QTimer.singleShot(delay_ms, lambda: self._schedule_video_geometry_probe(attempts - 1, delay_ms))

    def _prepare_runtime_subtitle_copy(self, subtitle_path: str) -> str | None:
        source_path = Path(subtitle_path)
        if not source_path.is_file():
            return None

        runtime_dir = Path(tempfile.gettempdir()) / "A1lPlayer" / "subtitles"
        runtime_dir.mkdir(parents=True, exist_ok=True)

        runtime_copy_path = runtime_dir / f"{source_path.stem}_{uuid.uuid4().hex}{source_path.suffix}"
        shutil.copyfile(source_path, runtime_copy_path)

        self._cleanup_runtime_subtitle_copy()
        return str(runtime_copy_path)

    def _cleanup_runtime_subtitle_copy(self):
        if not self._runtime_subtitle_copy_path:
            return
        self._remove_subtitle_copy(self._runtime_subtitle_copy_path)
        self._runtime_subtitle_copy_path = None

    def _remove_subtitle_copy(self, path: str | Path):
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
