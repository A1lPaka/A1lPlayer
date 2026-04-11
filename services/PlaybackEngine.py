import logging
import os
import shutil
from collections import deque
from pathlib import Path

import vlc

from PySide6.QtCore import QObject, QMetaObject, Qt, QTimer, Signal, Slot
from services.AppTempService import AppTempService

VLC_AUDIO_CHANNEL_MONO = 7
AUDIO_DEVICE_DEFAULT_ID = "__default__"


logger = logging.getLogger(__name__)


class PlaybackService(QObject):
    _AUDIO_SYNC_DELAY_MS = 150

    playing = Signal(int)
    paused = Signal(int)
    stopped = Signal(int)
    media_ended = Signal(int)
    playback_error = Signal(int, str, str)
    video_geometry_changed = Signal(int, int)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        AppTempService.cleanup_startup_orphans()
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
        self._current_request_id = 0
        self._queued_player_events: deque[tuple[str, int, str]] = deque()
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
        self._delayed_audio_sync_timer = QTimer(self)
        self._delayed_audio_sync_timer.setSingleShot(True)
        self._delayed_audio_sync_timer.setInterval(self._AUDIO_SYNC_DELAY_MS)
        self._delayed_audio_sync_timer.timeout.connect(self._apply_desired_audio_state)
        self._create_backend()

        self.playing.connect(self.sync_audio_to_player)

    def _create_backend(self):
        logger.info("Creating VLC playback backend")
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
        event_manager.event_attach(vlc.EventType.MediaPlayerEncounteredError, self._on_vlc_error_event)

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

    def load_media(self, media_path: str) -> int:
        self._cleanup_runtime_subtitle_copy()
        self._current_request_id += 1
        self._current_media_path = media_path
        self._last_video_geometry = None
        logger.info("Loading media into VLC | request_id=%s | media=%s", self._current_request_id, media_path)
        media = self.instance.media_new(media_path)
        self.player.set_media(media)
        return self._current_request_id

    def current_request_id(self) -> int:
        return self._current_request_id

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
        logger.info("Stopping playback | media=%s", self._current_media_path)
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
        except (AttributeError, TypeError, ValueError, vlc.VLCException):
            logger.debug("VLC video size is not available yet", exc_info=True)
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
            logger.warning(
                "Subtitle load skipped because no media is active or subtitle path is empty | subtitle=%s | media=%s",
                subtitle_path or "<empty>",
                self._current_media_path or "<none>",
            )
            return False

        previous_runtime_path = self._runtime_subtitle_copy_path
        previous_track_id = self.get_current_subtitle_track()
        runtime_path = self._prepare_runtime_subtitle_copy(subtitle_path)
        if runtime_path is None:
            logger.error("Failed to prepare runtime subtitle copy | subtitle=%s", subtitle_path)
            return False

        if self.player.video_set_subtitle_file(runtime_path) != 0:
            logger.error(
                "VLC failed to load subtitle file | subtitle=%s | runtime_copy=%s | media=%s",
                subtitle_path,
                runtime_path,
                self._current_media_path or "<none>",
            )
            self._remove_subtitle_copy(runtime_path)
            self._restore_subtitle_state(previous_runtime_path, previous_track_id)
            return False

        self._runtime_subtitle_copy_path = runtime_path
        if previous_runtime_path and previous_runtime_path != runtime_path:
            self._remove_subtitle_copy(previous_runtime_path)
        logger.info("Subtitle loaded into VLC | subtitle=%s | runtime_copy=%s", subtitle_path, runtime_path)
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

    def _apply_desired_audio_state(self):
        self.player.set_rate(self._desired_rate)
        self.player.audio_set_volume(self._desired_volume)
        self.player.audio_set_mute(self._desired_muted)
        self.player.audio_output_device_set(None, self._desired_audio_device_id)
        desired_channel = self._get_runtime_audio_channel(self._desired_audio_mode)
        if desired_channel is not None:
            self.player.audio_set_channel(desired_channel)

    def sync_audio_to_player(self):
        self._apply_desired_audio_state()
        self._delayed_audio_sync_timer.start()

    def _on_vlc_playing_event(self, event):
        self._queue_player_event("playing", self._current_request_id, self._current_media_path or "")

    def _on_vlc_paused_event(self, event):
        self._queue_player_event("paused", self._current_request_id, self._current_media_path or "")

    def _on_vlc_stopped_event(self, event):
        self._queue_player_event("stopped", self._current_request_id, self._current_media_path or "")

    def _on_vlc_media_ended_event(self, event):
        self._queue_player_event("ended", self._current_request_id, self._current_media_path or "")

    def _on_vlc_error_event(self, event):
        logger.error(
            "VLC reported playback error | request_id=%s | media=%s",
            self._current_request_id,
            self._current_media_path or "<none>",
        )
        self._queue_player_event("error", self._current_request_id, self._current_media_path or "")

    @Slot()
    def _flush_player_events_from_qt_thread(self):
        while self._queued_player_events:
            event_name, request_id, media_path = self._queued_player_events.popleft()
            if event_name == "playing":
                self.playing.emit(request_id)
                if request_id == self._current_request_id:
                    self._schedule_video_geometry_probe()
                continue
            if event_name == "paused":
                self.paused.emit(request_id)
                continue
            if event_name == "stopped":
                self.stopped.emit(request_id)
                continue
            if event_name == "ended":
                self.media_ended.emit(request_id)
                continue

            self._cleanup_runtime_subtitle_copy()
            logger.error("Playback failure path reached | request_id=%s | media=%s", request_id, media_path or "<unknown>")
            self.playback_error.emit(
                request_id,
                media_path,
                "Failed to open or play this media file. The file may be corrupted or unsupported.",
            )

    def _queue_player_event(self, event_name: str, request_id: int, media_path: str):
        self._queued_player_events.append((event_name, int(request_id), str(media_path)))
        QMetaObject.invokeMethod(self, "_flush_player_events_from_qt_thread", Qt.QueuedConnection)

    def _disable_vout_input(self):
        try:
            self.player.video_set_mouse_input(False)
        except (AttributeError, TypeError, ValueError, vlc.VLCException):
            logger.debug("Failed to disable VLC mouse input", exc_info=True)

        try:
            self.player.video_set_key_input(False)
        except (AttributeError, TypeError, ValueError, vlc.VLCException):
            logger.debug("Failed to disable VLC key input", exc_info=True)

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
            logger.error("Subtitle source file does not exist | subtitle=%s", subtitle_path)
            return None

        runtime_copy_path = AppTempService.create_runtime_subtitle_copy_path(source_path)
        try:
            shutil.copyfile(source_path, runtime_copy_path)
        except OSError:
            logger.exception(
                "Failed to create runtime subtitle copy | subtitle=%s | runtime_copy=%s",
                subtitle_path,
                runtime_copy_path,
            )
            return None
        return str(runtime_copy_path)

    def _cleanup_runtime_subtitle_copy(self):
        if not self._runtime_subtitle_copy_path:
            return
        self._remove_subtitle_copy(self._runtime_subtitle_copy_path)
        self._runtime_subtitle_copy_path = None

    def _restore_subtitle_state(
        self,
        previous_runtime_path: str | None,
        previous_track_id: int,
    ):
        if previous_runtime_path:
            self.player.video_set_subtitle_file(previous_runtime_path)
            return

        if previous_track_id != -1:
            self.player.video_set_spu(int(previous_track_id))

    def _remove_subtitle_copy(self, path: str | Path):
        AppTempService.remove_file_if_exists(path, log_context="runtime subtitle cleanup")
