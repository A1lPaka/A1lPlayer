from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from PySide6.QtCore import QObject, QTimer, Signal

from services.PlaybackEngine import PlaybackService
from models.PlaybackPlaylist import PlaylistState


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PlaybackInterruption:
    paused_by_owner: bool
    media_path: str | None
    request_id: int


class PlaybackInterruptionLease:
    def __init__(
        self,
        controller: "PlayerPlaybackController",
        owner: str,
        *,
        emit_pause_requested: bool = True,
    ):
        self._controller = controller
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
        self._paused_playback = self._controller.pause_for_interruption(
            self._owner,
            emit_pause_requested=self._emit_pause_requested,
        )
        if self._paused_playback:
            self._controller.pause()
        return self._paused_playback

    def release(self, *, resume_playback: bool = True):
        if not self._acquired:
            return

        self._acquired = False
        self._paused_playback = False
        if resume_playback:
            self._controller.resume_after_interruption(self._owner)
            return

        self._controller.clear_interruption(self._owner)


class PlayerPlaybackController(QObject):
    STATE_STOPPED = "stopped"
    STATE_OPENING = "opening"
    STATE_PAUSED = "paused"
    STATE_PLAYING = "playing"
    _SEEK_INTERRUPTION_OWNER = "seek"

    playback_state_changed = Signal(str)
    media_finished = Signal(str)
    media_assigned = Signal(str)
    media_confirmed = Signal(int, str)
    current_media_changed = Signal(str)
    active_media_changed = Signal(object)
    playback_error = Signal(int, str, str)
    video_geometry_changed = Signal(int, int)
    pause_requested = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.engine = PlaybackService(self)
        self.playlist = PlaylistState()

        self._pending_start_position_ms = 0
        self._playback_state = self.STATE_STOPPED
        self._active_request_id = 0
        self._media_assigned = False
        self._media_confirmed_loaded = False
        self._last_confirmed_media_path: str | None = None
        self._active_media_path: str | None = None
        self._is_shutdown = False
        self._playback_interruptions: dict[str, _PlaybackInterruption] = {}

        self.engine.playing.connect(self._handle_engine_playing)
        self.engine.paused.connect(self._handle_engine_paused)
        self.engine.stopped.connect(self._handle_engine_stopped)
        self.engine.media_ended.connect(self._handle_media_end)
        self.engine.playback_error.connect(self._handle_engine_error)
        self.engine.video_geometry_changed.connect(self.video_geometry_changed)

    def shutdown(self):
        if self._is_shutdown:
            return
        self._is_shutdown = True
        self._pending_start_position_ms = 0
        self._clear_playback_interruptions()
        self._active_request_id = 0
        self._media_assigned = False
        self._media_confirmed_loaded = False
        self._last_confirmed_media_path = None
        self._set_active_media_path(None)
        self._set_playback_state(self.STATE_STOPPED)
        self.engine.shutdown()

    def current_media_path(self) -> str | None:
        return self.playlist.current_path()

    def current_request_id(self) -> int:
        return self._active_request_id

    def playback_state(self) -> str:
        return self._playback_state

    def get_session_snapshot(self) -> dict[str, int | str] | None:
        if not self._media_confirmed_loaded:
            return None

        current_path = self.current_media_path()
        if current_path is None:
            return None

        return {
            "path": current_path,
            "position_ms": self.engine.get_time(),
            "total_ms": self.engine.get_length(),
        }

    def load_playlist(self, file_paths: list[str], start_index: int = 0) -> bool:
        logger.info("Loading playlist | count=%s | start_index=%s", len(file_paths), start_index)
        if not self.playlist.load(file_paths, start_index=start_index):
            logger.warning("Playlist load rejected | count=%s | start_index=%s", len(file_paths), start_index)
            return False
        if self._load_from_playlist_index(self.playlist.current_index, step=1, wrap=True):
            return True

        logger.warning("Playlist load failed because no playable item was found | count=%s | start_index=%s", len(file_paths), start_index)
        self.playlist.clear()
        return False

    def open_paths(self, file_paths: list[str], start_index: int = 0, start_position_ms: int = 0) -> bool:
        logger.info(
            "Opening playback paths | count=%s | start_index=%s | start_position_ms=%s",
            len(file_paths),
            start_index,
            start_position_ms,
        )
        if not self.load_playlist(file_paths, start_index=start_index):
            return False
        self.play_loaded_media(start_position_ms=start_position_ms)
        return True

    def play_loaded_media(self, start_position_ms: int = 0):
        self._pending_start_position_ms = max(0, int(start_position_ms))
        logger.info(
            "Starting playback for loaded media | media=%s | pending_start_position_ms=%s",
            self.current_media_path(),
            self._pending_start_position_ms,
        )
        self.engine.sync_audio_to_player()
        self.engine.play()
        self._set_playback_state(self.STATE_OPENING)

    def toggle_play_pause(self):
        if self.engine.is_playing():
            self.pause()
            return

        self.play()

    def play(self):
        self.engine.sync_audio_to_player()
        self.engine.play()
        self._set_playback_state(self.STATE_OPENING)

    def pause(self):
        self.pause_requested.emit()
        self.engine.pause()

    def stop(self):
        self._pending_start_position_ms = 0
        self._clear_playback_interruptions()
        logger.info("Playback stop requested | media=%s", self.current_media_path())
        self._media_confirmed_loaded = False
        self._last_confirmed_media_path = None
        self._set_active_media_path(None)
        self.engine.stop()
        self._set_playback_state(self.STATE_STOPPED)

    def play_previous(self) -> bool:
        current_index = self.playlist.current_index
        if current_index < 0 or not self.playlist.has_multiple():
            return False
        start_index = (current_index - 1) % len(self.playlist.paths)
        if not self._load_from_playlist_index(start_index, step=-1, wrap=True):
            return False
        self.play_loaded_media()
        return True

    def play_next(self) -> bool:
        current_index = self.playlist.current_index
        if current_index < 0 or not self.playlist.has_multiple():
            return False
        start_index = (current_index + 1) % len(self.playlist.paths)
        if not self._load_from_playlist_index(start_index, step=1, wrap=True):
            return False
        self.play_loaded_media()
        return True

    def begin_seek(self):
        if self.pause_for_interruption(self._SEEK_INTERRUPTION_OWNER, emit_pause_requested=False):
            self.engine.pause()

    def seek_to_ratio(self, value: float):
        if self.engine.is_seekable():
            self.engine.set_position(max(0.0, min(1.0, value)))

    def finish_seek(self):
        self.resume_after_interruption(self._SEEK_INTERRUPTION_OWNER)

    def seek_by_hold(self, direction: str):
        current_ms = self.engine.get_time()
        if current_ms < 0:
            return

        step_ms = -10_000 if direction == "left" else 10_000
        new_ms = max(0, current_ms + step_ms)
        total_ms = self.engine.get_length()
        if total_ms > 0:
            self.engine.set_position(new_ms / total_ms)

    def seek_by_ms(self, delta_ms: int):
        current_ms = self.engine.get_time()
        if current_ms < 0:
            return

        total_ms = self.engine.get_length()
        target_ms = max(0, current_ms + int(delta_ms))
        if total_ms > 0:
            target_ms = min(target_ms, total_ms)
            self.engine.set_position(target_ms / total_ms)

    def has_media_loaded(self) -> bool:
        return self._media_confirmed_loaded

    def has_assigned_media(self) -> bool:
        return self._media_assigned

    def is_playing(self) -> bool:
        return self.engine.is_playing()

    def create_interruption_lease(
        self,
        owner: str,
        *,
        emit_pause_requested: bool = True,
    ) -> PlaybackInterruptionLease:
        return PlaybackInterruptionLease(
            self,
            owner,
            emit_pause_requested=emit_pause_requested,
        )

    def pause_for_interruption(self, owner: str, *, emit_pause_requested: bool = True) -> bool:
        interruption = self._playback_interruptions.get(owner)
        if interruption is not None:
            return interruption.paused_by_owner

        paused_by_owner = self.engine.is_playing()
        self._playback_interruptions[owner] = _PlaybackInterruption(
            paused_by_owner=paused_by_owner,
            media_path=self.current_media_path(),
            request_id=self._active_request_id,
        )
        if paused_by_owner and emit_pause_requested:
            self.pause_requested.emit()
        return paused_by_owner

    def resume_after_interruption(self, owner: str):
        interruption = self._playback_interruptions.pop(owner, None)
        if interruption is None or not interruption.paused_by_owner:
            return
        if self._playback_interruptions:
            return
        if self.engine.is_playing():
            return
        if interruption.request_id != self._active_request_id:
            return
        if interruption.media_path != self.current_media_path():
            return
        self.play()

    def clear_interruption(self, owner: str):
        self._playback_interruptions.pop(owner, None)

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
        logger.info("Playback controller received subtitle open request | subtitle=%s | media=%s", subtitle_path, self.current_media_path())
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

    def _handle_media_end(self, request_id: int):
        if request_id != self._active_request_id:
            return

        finished_path = self.current_media_path()
        logger.info("Media playback finished | request_id=%s | media=%s", request_id, finished_path)
        if finished_path:
            self.media_finished.emit(finished_path)

        if self._playback_state == self.STATE_STOPPED:
            logger.info("Media completion handling stopped by external owner | request_id=%s", request_id)
            return

        if self._play_next_from_playlist():
            return

        self.stop()

    def _play_next_from_playlist(self) -> bool:
        current_index = self.playlist.current_index
        if current_index < 0:
            logger.info("Playlist has no next item for linear advance")
            return False
        start_index = current_index + 1
        if not self._load_from_playlist_index(start_index, step=1, wrap=False):
            logger.info("Playlist has no playable next item for linear advance")
            return False
        logger.info("Advancing to next playlist item | media=%s", self.playlist.current_path())
        self.play_loaded_media()
        return True

    def _load_from_playlist_index(self, start_index: int, *, step: int, wrap: bool) -> bool:
        playable_index = self._find_playable_index(start_index, step=step, wrap=wrap)
        if playable_index is None:
            return False
        self.playlist.set_current_index(playable_index)
        return self._load_current_media()

    def _find_playable_index(self, start_index: int, *, step: int, wrap: bool) -> int | None:
        paths = self.playlist.paths
        count = len(paths)
        if count == 0 or start_index < 0 or start_index >= count:
            return None

        index = start_index
        checked = 0
        while checked < count and 0 <= index < count:
            media_path = paths[index]
            if os.path.exists(media_path):
                return index

            logger.warning("Skipping unavailable playlist item | index=%s | media=%s", index, media_path)
            checked += 1
            if checked >= count:
                break

            next_index = index + step
            if wrap:
                index = next_index % count
                continue
            if next_index < 0 or next_index >= count:
                break
            index = next_index

        return None

    def _load_current_media(self) -> bool:
        media_path = self.playlist.current_path()
        if media_path is None:
            logger.warning("Current playlist item is empty; cannot load media")
            return False

        self._clear_playback_interruptions()
        if self._active_media_path is not None and self._active_media_path != media_path:
            self._set_active_media_path(None)
        self._media_confirmed_loaded = False
        self._last_confirmed_media_path = None
        request_id = self.engine.load_media(media_path)
        self._active_request_id = request_id
        self._media_assigned = True
        logger.info("Media assigned to playback engine | request_id=%s | media=%s", request_id, media_path)
        self._set_playback_state(self.STATE_OPENING)
        self.media_assigned.emit(media_path)
        return True

    def _handle_engine_playing(self, request_id: int):
        if request_id != self._active_request_id:
            return

        current_path = self.current_media_path()
        self._media_confirmed_loaded = current_path is not None
        logger.info("Playback confirmed by engine | request_id=%s | media=%s", request_id, current_path)
        self._set_playback_state(self.STATE_PLAYING)
        if current_path and current_path != self._last_confirmed_media_path:
            self._last_confirmed_media_path = current_path
            self._set_active_media_path(current_path)
            self.media_confirmed.emit(request_id, current_path)
            self.current_media_changed.emit(current_path)
        if self._pending_start_position_ms > 0:
            self._apply_pending_start_position()

    def _handle_engine_paused(self, request_id: int):
        if request_id != self._active_request_id:
            return
        self._set_playback_state(self.STATE_PAUSED)

    def _handle_engine_stopped(self, request_id: int):
        if request_id != self._active_request_id:
            return
        self._clear_playback_interruptions()
        self._media_confirmed_loaded = False
        self._last_confirmed_media_path = None
        self._set_active_media_path(None)
        self._set_playback_state(self.STATE_STOPPED)

    def _handle_engine_error(self, request_id: int, media_path: str, message: str):
        if request_id != self._active_request_id:
            return

        logger.error(
            "Playback controller received fatal engine error | request_id=%s | media=%s | message=%s",
            request_id,
            media_path,
            message,
        )
        self._pending_start_position_ms = 0
        self._clear_playback_interruptions()
        self._active_request_id = 0
        self._media_assigned = False
        self._media_confirmed_loaded = False
        self._last_confirmed_media_path = None
        self._set_active_media_path(None)
        self._set_playback_state(self.STATE_STOPPED)
        self.playback_error.emit(request_id, media_path, message)

    def _apply_pending_start_position(self, attempts: int = 8, delay_ms: int = 100):
        if self._pending_start_position_ms <= 0:
            return
        if attempts <= 0:
            logger.warning("Timed out applying pending start position | media=%s", self.current_media_path())
            self._pending_start_position_ms = 0
            return

        total_ms = self.engine.get_length()
        if total_ms > 0:
            logger.info(
                "Applying pending start position | media=%s | position_ms=%s | total_ms=%s",
                self.current_media_path(),
                self._pending_start_position_ms,
                total_ms,
            )
            self.engine.set_time(min(self._pending_start_position_ms, total_ms))
            self._pending_start_position_ms = 0
            return

        QTimer.singleShot(delay_ms, lambda: self._apply_pending_start_position(attempts - 1, delay_ms))

    def _set_playback_state(self, state: str):
        if self._playback_state == state:
            return
        self._playback_state = state
        self.playback_state_changed.emit(state)

    def _set_active_media_path(self, media_path: str | None):
        if self._active_media_path == media_path:
            return
        self._active_media_path = media_path
        self.active_media_changed.emit(media_path)

    def _clear_playback_interruptions(self):
        self._playback_interruptions.clear()
