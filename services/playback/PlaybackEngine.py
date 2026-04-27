import logging
import os
import shutil
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

try:
    import vlc
except (ImportError, OSError) as exc:
    vlc = None
    _VLC_IMPORT_ERROR = exc
else:
    _VLC_IMPORT_ERROR = None

from PySide6.QtCore import QObject, QMetaObject, Qt, QTimer, Signal, Slot
from services.app.AppTempService import AppTempService

VLC_AUDIO_CHANNEL_MONO = 7
AUDIO_DEVICE_DEFAULT_ID = "__default__"
PLAYBACK_BACKEND_UNAVAILABLE_MESSAGE = (
    "The VLC playback backend is unavailable. Please install VLC or check that libVLC is available on PATH."
)

_VLC_ERRORS = (AttributeError, TypeError, ValueError, OSError)
if vlc is not None:
    _VLC_ERRORS = (*_VLC_ERRORS, getattr(vlc, "VLCException", Exception))


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PlaybackToken:
    request_id: int
    media_path: str


class PlaybackService(QObject):
    _AUDIO_SYNC_DELAY_MS = 150
    _FAILED_RUNTIME_SUBTITLE_CLEANUP_DELAY_MS = 1500

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
        self._current_playback_token: _PlaybackToken | None = None
        self._current_media = None
        self._current_media_event_manager = None
        self.instance = None
        self.player = None
        self._backend_error_message: str | None = None
        self._queued_player_events: deque[tuple[str, int, str]] = deque()
        self._queued_player_events_lock = threading.Lock()
        self._player_events_flush_scheduled = False
        self._is_shutdown = False
        self._audio_modes = self._build_audio_modes()
        self._delayed_audio_sync_timer = QTimer(self)
        self._delayed_audio_sync_timer.setSingleShot(True)
        self._delayed_audio_sync_timer.setInterval(self._AUDIO_SYNC_DELAY_MS)
        self._delayed_audio_sync_timer.timeout.connect(self._apply_desired_audio_state)
        self._create_backend()

        self.playing.connect(self.sync_audio_to_player)

    def _create_backend(self):
        if vlc is None:
            self._backend_error_message = PLAYBACK_BACKEND_UNAVAILABLE_MESSAGE
            logger.error(
                "VLC Python bindings could not be imported | error=%s | PATH=%s",
                _VLC_IMPORT_ERROR,
                os.environ.get("PATH", ""),
            )
            return

        logger.info("Creating VLC playback backend")
        try:
            self.instance = vlc.Instance()
            self.player = self.instance.media_player_new()
        except _VLC_ERRORS as exc:
            self.instance = None
            self.player = None
            self._backend_error_message = PLAYBACK_BACKEND_UNAVAILABLE_MESSAGE
            logger.exception(
                "Failed to create VLC playback backend | error=%s | PATH=%s",
                exc,
                os.environ.get("PATH", ""),
            )
            return

        if self._bound_win_id is not None:
            self.bind_video_output(self._bound_win_id)

    def _has_backend(self) -> bool:
        return not self._is_shutdown and getattr(self, "player", None) is not None and getattr(self, "instance", None) is not None

    def is_backend_available(self) -> bool:
        return self._has_backend()

    def backend_error_message(self) -> str | None:
        return self._backend_error_message

    def shutdown(self):
        if self._is_shutdown:
            logger.debug("Playback backend shutdown skipped because it already completed")
            return

        logger.info("Shutting down VLC playback backend | media=%s", self._current_media_path or "<none>")
        self._is_shutdown = True
        self._delayed_audio_sync_timer.stop()
        with self._queued_player_events_lock:
            self._queued_player_events.clear()
            self._player_events_flush_scheduled = False
        self._detach_current_media_event_handlers()
        player = getattr(self, "player", None)
        if player is not None:
            try:
                player.stop()
            except _VLC_ERRORS:
                logger.debug("Failed to stop VLC player during shutdown", exc_info=True)
        self._cleanup_runtime_subtitle_copy()
        self._release_backend()
        self._current_media_path = None
        self._current_playback_token = None
        self._bound_win_id = None
        self._last_video_geometry = None

    def _release_backend(self):
        player = getattr(self, "player", None)
        instance = getattr(self, "instance", None)

        if player is not None:
            try:
                player.release()
            except _VLC_ERRORS:
                logger.debug("Failed to release VLC player during shutdown", exc_info=True)
            self.player = None

        if instance is not None:
            try:
                instance.release()
            except _VLC_ERRORS:
                logger.debug("Failed to release VLC instance during shutdown", exc_info=True)
            self.instance = None

    def _build_audio_modes(self) -> dict[str, dict[str, int]]:
        if vlc is None:
            return self._fallback_audio_modes()

        try:
            return {
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
        except _VLC_ERRORS:
            logger.debug("Falling back to raw VLC audio channel ids", exc_info=True)
            return self._fallback_audio_modes()

    def _fallback_audio_modes(self) -> dict[str, dict[str, int]]:
        return {
            "stereo": {"channel": 1},
            "reverse_stereo": {"channel": 2},
            "left": {"channel": 3},
            "right": {"channel": 4},
            "mono": {"channel": VLC_AUDIO_CHANNEL_MONO},
        }

    def _attach_media_event_handlers(self, media, token: _PlaybackToken):
        event_manager = media.event_manager()
        event_manager.event_attach(vlc.EventType.MediaStateChanged, self._on_vlc_media_state_changed_event, token)
        self._current_media = media
        self._current_media_event_manager = event_manager

    def _detach_current_media_event_handlers(self):
        event_manager = self._current_media_event_manager
        if event_manager is None:
            return
        try:
            event_manager.event_detach(vlc.EventType.MediaStateChanged)
        except _VLC_ERRORS:
            logger.debug("Failed to detach VLC media event handler", exc_info=True)
        self._current_media_event_manager = None
        self._current_media = None

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
        if not self._has_backend():
            return
        self._bound_win_id = win_id
        if os.name == "nt":
            self.player.set_hwnd(win_id)
        elif os.name == "posix":
            self.player.set_xwindow(win_id)
        self._disable_vout_input()

    def load_media(self, media_path: str) -> int:
        if not self._has_backend():
            self._current_request_id += 1
            self._current_media_path = media_path
            request_id = self._current_request_id
            message = self._backend_error_message or PLAYBACK_BACKEND_UNAVAILABLE_MESSAGE
            logger.warning(
                "Media load rejected because playback backend is unavailable | request_id=%s | media=%s | reason=%s",
                request_id,
                media_path,
                message,
            )
            QTimer.singleShot(
                0,
                lambda request_id=request_id, media_path=media_path, message=message: self._emit_backend_unavailable_error(
                    request_id,
                    media_path,
                    message,
                ),
            )
            return request_id
        self._cleanup_runtime_subtitle_copy()
        self._current_request_id += 1
        self._current_media_path = media_path
        self._current_playback_token = _PlaybackToken(self._current_request_id, media_path)
        self._last_video_geometry = None
        logger.info("Loading media into VLC | request_id=%s | media=%s", self._current_request_id, media_path)
        self._detach_current_media_event_handlers()
        media = self.instance.media_new(media_path)
        self._attach_media_event_handlers(media, self._current_playback_token)
        self.player.set_media(media)
        return self._current_request_id

    def _emit_backend_unavailable_error(self, request_id: int, media_path: str, message: str):
        if self._is_shutdown or request_id != self._current_request_id:
            return
        self.playback_error.emit(request_id, media_path, message)

    def current_request_id(self) -> int:
        return self._current_request_id

    def get_media(self):
        if not self._has_backend():
            return None
        return self.player.get_media()

    def is_playing(self) -> bool:
        if not self._has_backend():
            return False
        return self.player.get_state() == vlc.State.Playing

    def is_seekable(self) -> bool:
        if not self._has_backend():
            return False
        return self.player.is_seekable()

    def play(self):
        if not self._has_backend():
            return
        self.player.play()

    def pause(self):
        if not self._has_backend():
            return
        self.player.pause()

    def stop(self):
        logger.info("Stopping playback | media=%s", self._current_media_path)
        self._cleanup_runtime_subtitle_copy()
        if not self._has_backend():
            return
        self.player.stop()

    def set_time(self, position_ms: int):
        if not self._has_backend():
            return
        self.player.set_time(position_ms)

    def get_time(self) -> int:
        if not self._has_backend():
            return 0
        return int(self.player.get_time())

    def get_length(self) -> int:
        if not self._has_backend():
            return 0
        return int(self.player.get_length())

    def get_video_dimensions(self) -> tuple[int, int] | None:
        if not self._has_backend():
            return None
        try:
            size = self.player.video_get_size(0)
        except _VLC_ERRORS:
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
        if not self._has_backend():
            return
        self.player.set_position(position)

    def set_rate(self, rate: float) -> bool:
        clamped_rate = max(0.25, min(4.0, float(rate)))
        self._desired_rate = clamped_rate
        if not self._has_backend():
            return False
        return self.player.set_rate(clamped_rate) == 0

    def get_rate(self) -> float:
        return self._desired_rate

    def get_audio_tracks(self):
        if not self._has_backend():
            return []
        return self.player.audio_get_track_description() or []

    def get_current_audio_track(self) -> int:
        if not self._has_backend():
            return -1
        return int(self.player.audio_get_track())

    def set_audio_track(self, track_id: int) -> bool:
        if not self._has_backend():
            return False
        return self.player.audio_set_track(int(track_id)) == 0

    def get_audio_devices(self) -> list[tuple[str, str]]:
        if not self._has_backend():
            return [(AUDIO_DEVICE_DEFAULT_ID, "Default Device")]
        devices: list[tuple[str, str]] = []
        seen_device_ids: set[str] = set()
        device_list = self.player.audio_output_device_enum()
        try:
            for device_item in self._iter_vlc_linked_list(device_list):
                raw_device_id = self._decode_vlc_text(device_item.device)
                device_title = self._decode_vlc_text(device_item.description)
                normalized_device_id = raw_device_id or AUDIO_DEVICE_DEFAULT_ID
                normalized_title = device_title or "Default Device"

                if normalized_device_id in seen_device_ids:
                    continue

                devices.append((normalized_device_id, normalized_title))
                seen_device_ids.add(normalized_device_id)
        finally:
            if device_list:
                vlc.libvlc_audio_output_device_list_release(device_list)

        if AUDIO_DEVICE_DEFAULT_ID not in seen_device_ids:
            devices.insert(0, (AUDIO_DEVICE_DEFAULT_ID, "Default Device"))

        return devices

    def get_current_audio_device(self) -> str:
        if not self._has_backend():
            return AUDIO_DEVICE_DEFAULT_ID
        current_device_id = self._decode_vlc_text(self.player.audio_output_device_get())
        return current_device_id or AUDIO_DEVICE_DEFAULT_ID

    def set_audio_device(self, device_id: str) -> bool:
        normalized_device_id = None if device_id == AUDIO_DEVICE_DEFAULT_ID else str(device_id)
        self._desired_audio_device_id = normalized_device_id
        if not self._has_backend():
            return False
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
        if not self._has_backend():
            return False
        if runtime_channel is not None:
            return self.player.audio_set_channel(runtime_channel) == 0

        return True

    def get_subtitle_tracks(self):
        if not self._has_backend():
            return []
        return self.player.video_get_spu_description() or []

    def get_current_subtitle_track(self) -> int:
        if not self._has_backend():
            return -1
        return int(self.player.video_get_spu())

    def set_subtitle_track(self, track_id: int) -> bool:
        if not self._has_backend():
            return False
        return self.player.video_set_spu(int(track_id)) == 0

    def open_subtitle_file(self, subtitle_path: str) -> bool:
        if not self._has_backend():
            return False
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

        if not self._attach_subtitle_file(runtime_path, subtitle_path_for_logs=subtitle_path):
            runtime_uri = self._build_vlc_file_uri(runtime_path)
            logger.error(
                "VLC failed to load subtitle file | subtitle=%s | runtime_copy=%s | runtime_uri=%s | runtime_copy_exists=%s | media=%s",
                subtitle_path,
                runtime_path,
                runtime_uri,
                Path(runtime_path).is_file(),
                self._current_media_path or "<none>",
            )
            # libVLC can attempt to consume the file asynchronously after the initial call returns.
            self._schedule_failed_runtime_subtitle_cleanup(runtime_path)
            self._restore_subtitle_state(previous_runtime_path, previous_track_id)
            return False

        self._runtime_subtitle_copy_path = runtime_path
        if previous_runtime_path and previous_runtime_path != runtime_path:
            self._remove_subtitle_copy(previous_runtime_path)
        logger.info("Subtitle loaded into VLC | subtitle=%s | runtime_copy=%s", subtitle_path, runtime_path)
        return True

    def set_volume(self, volume: int):
        self._desired_volume = max(0, min(100, volume))
        if not self._has_backend():
            return
        self.player.audio_set_volume(self._desired_volume)

    def get_desired_volume(self) -> int:
        return self._desired_volume

    def set_muted(self, muted: bool):
        self._desired_muted = bool(muted)
        if not self._has_backend():
            return
        self.player.audio_set_mute(self._desired_muted)

    def is_muted(self) -> bool:
        return self._desired_muted

    def get_last_volume_before_mute(self) -> int:
        return self._last_volume_before_mute

    def set_last_volume_before_mute(self, volume: int):
        self._last_volume_before_mute = max(0, min(100, volume))

    def _apply_desired_audio_state(self):
        if not self._has_backend():
            return
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

    def _on_vlc_media_state_changed_event(self, event, token: _PlaybackToken):
        state = int(event.u.new_state)
        if state == int(vlc.State.Playing.value):
            self._queue_player_event("playing", token.request_id, token.media_path)
            return
        if state == int(vlc.State.Paused.value):
            self._queue_player_event("paused", token.request_id, token.media_path)
            return
        if state == int(vlc.State.Stopped.value):
            self._queue_player_event("stopped", token.request_id, token.media_path)
            return
        if state == int(vlc.State.Ended.value):
            self._queue_player_event("ended", token.request_id, token.media_path)
            return
        if state != int(vlc.State.Error.value):
            return

        logger.error("VLC reported playback error | request_id=%s | media=%s", token.request_id, token.media_path)
        self._queue_player_event("error", token.request_id, token.media_path)

    @Slot()
    def _flush_player_events_from_qt_thread(self):
        while True:
            with self._queued_player_events_lock:
                if self._is_shutdown:
                    self._queued_player_events.clear()
                    self._player_events_flush_scheduled = False
                    return
                if not self._queued_player_events:
                    self._player_events_flush_scheduled = False
                    return
                event_name, request_id, media_path = self._queued_player_events.popleft()
            if self._is_shutdown:
                with self._queued_player_events_lock:
                    self._queued_player_events.clear()
                    self._player_events_flush_scheduled = False
                return
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

            if request_id != self._current_request_id:
                logger.debug(
                    "Ignoring stale playback failure event | request_id=%s | active_request_id=%s | media=%s",
                    request_id,
                    self._current_request_id,
                    media_path or "<unknown>",
                )
                continue

            self._cleanup_runtime_subtitle_copy()
            logger.error("Playback failure path reached | request_id=%s | media=%s", request_id, media_path or "<unknown>")
            self.playback_error.emit(
                request_id,
                media_path,
                "Failed to open or play this media file. The file may be corrupted or unsupported.",
            )

    def _queue_player_event(self, event_name: str, request_id: int, media_path: str):
        if self._is_shutdown:
            return
        with self._queued_player_events_lock:
            self._queued_player_events.append((event_name, int(request_id), str(media_path)))
            if self._player_events_flush_scheduled:
                return
            self._player_events_flush_scheduled = True
        QMetaObject.invokeMethod(self, "_flush_player_events_from_qt_thread", Qt.QueuedConnection)

    def _disable_vout_input(self):
        if not self._has_backend():
            return
        try:
            self.player.video_set_mouse_input(False)
        except _VLC_ERRORS:
            logger.debug("Failed to disable VLC mouse input", exc_info=True)

        try:
            self.player.video_set_key_input(False)
        except _VLC_ERRORS:
            logger.debug("Failed to disable VLC key input", exc_info=True)

    def _schedule_video_geometry_probe(self, attempts: int = 12, delay_ms: int = 120):
        if self._is_shutdown:
            return
        if attempts <= 0:
            return

        geometry = self.get_video_dimensions()
        if geometry is not None:
            if geometry != self._last_video_geometry:
                self._last_video_geometry = geometry
                self.video_geometry_changed.emit(*geometry)
            return

        QTimer.singleShot(delay_ms, lambda: self._continue_video_geometry_probe(attempts - 1, delay_ms))

    def _continue_video_geometry_probe(self, attempts: int, delay_ms: int):
        if self._is_shutdown:
            return
        self._schedule_video_geometry_probe(attempts, delay_ms)

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

    def _build_vlc_file_uri(self, path: str | Path) -> str:
        resolved_path = Path(path).resolve()
        normalized_posix_path = resolved_path.as_posix()
        if not normalized_posix_path.startswith("/"):
            normalized_posix_path = f"/{normalized_posix_path}"
        return f"file://{quote(normalized_posix_path)}"

    def _attach_subtitle_file(self, subtitle_path: str | Path, *, subtitle_path_for_logs: str | None = None) -> bool:
        if not self._has_backend():
            return False
        subtitle_runtime_path = str(subtitle_path)
        runtime_uri = self._build_vlc_file_uri(subtitle_runtime_path)

        # Reset the active SPU track before attaching a fresh external subtitle file.
        self.player.video_set_spu(-1)
        subtitle_load_result = self.player.add_slave(vlc.MediaSlaveType.subtitle, runtime_uri, True)
        if subtitle_load_result == 0:
            return True

        logger.warning(
            "VLC add_slave subtitle attach failed; falling back to deprecated video_set_subtitle_file | subtitle=%s | runtime_copy=%s | runtime_uri=%s | media=%s",
            subtitle_path_for_logs or subtitle_runtime_path,
            subtitle_runtime_path,
            runtime_uri,
            self._current_media_path or "<none>",
        )
        return self.player.video_set_subtitle_file(subtitle_runtime_path) == 0

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
        if not self._has_backend():
            return
        if previous_runtime_path:
            if not self._attach_subtitle_file(previous_runtime_path):
                logger.warning(
                    "Failed to restore previous subtitle attachment | runtime_copy=%s | media=%s",
                    previous_runtime_path,
                    self._current_media_path or "<none>",
                )

        if previous_track_id >= -1:
            self.player.video_set_spu(int(previous_track_id))

    def _remove_subtitle_copy(self, path: str | Path):
        AppTempService.remove_file_if_exists(path, log_context="runtime subtitle cleanup")

    def _schedule_failed_runtime_subtitle_cleanup(self, runtime_path: str):
        def cleanup_failed_runtime_copy():
            if self._runtime_subtitle_copy_path == runtime_path:
                return
            self._remove_subtitle_copy(runtime_path)

        QTimer.singleShot(self._FAILED_RUNTIME_SUBTITLE_CLEANUP_DELAY_MS, cleanup_failed_runtime_copy)
