from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QWidget


class SignalRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)


class FakePlaybackForSubtitle(QObject):
    media_confirmed = Signal(int, str)
    playback_error = Signal(int, str, str)
    media_finished = Signal(str)
    playback_state_changed = Signal(str)
    pause_requested = Signal()

    STATE_STOPPED = "stopped"

    def __init__(self):
        super().__init__()
        self._media_path = None
        self._request_id = None
        self._is_playing = False
        self.opened_subtitles = []
        self.open_subtitle_result = True
        self._has_media_loaded = False
        self._session_snapshot = None
        self.last_open_paths = None
        self.open_paths_result = True
        self.pause_calls = 0
        self.play_calls = 0
        self.interruptions = {}

    def current_media_path(self):
        return self._media_path

    def current_request_id(self):
        return self._request_id

    def is_playing(self):
        return self._is_playing

    def pause(self):
        self.pause_calls += 1
        self._is_playing = False

    def play(self):
        self.play_calls += 1
        self._is_playing = True

    def pause_for_interruption(self, owner: str, *, emit_pause_requested: bool = True):
        interruption = self.interruptions.get(owner)
        if interruption is not None:
            return interruption["paused_by_owner"]

        paused_by_owner = self._is_playing
        self.interruptions[owner] = {
            "paused_by_owner": paused_by_owner,
            "media_path": self._media_path,
            "request_id": self._request_id,
            "emit_pause_requested": emit_pause_requested,
        }
        if paused_by_owner and emit_pause_requested:
            self.pause_requested.emit()
        return paused_by_owner

    def resume_after_interruption(self, owner: str):
        interruption = self.interruptions.pop(owner, None)
        if interruption is None or not interruption["paused_by_owner"]:
            return
        if self.interruptions:
            return
        if self._is_playing:
            return
        if interruption["media_path"] != self._media_path:
            return
        if interruption["request_id"] != self._request_id:
            return
        self.play()

    def clear_interruption(self, owner: str):
        self.interruptions.pop(owner, None)

    def open_subtitle_file(self, subtitle_path: str) -> bool:
        self.opened_subtitles.append(subtitle_path)
        return self.open_subtitle_result

    def has_media_loaded(self):
        return self._has_media_loaded

    def playback_state(self):
        return self.STATE_STOPPED if not self._has_media_loaded else "playing"

    def get_session_snapshot(self):
        return self._session_snapshot

    def open_paths(self, file_paths: list[str], start_index: int = 0, start_position_ms: int = 0):
        self.last_open_paths = {
            "file_paths": list(file_paths),
            "start_index": start_index,
            "start_position_ms": start_position_ms,
        }
        if not self.open_paths_result:
            return False
        self._request_id = 101
        return True


class FakePlayerWindow(QObject):
    def __init__(self):
        super().__init__()
        self.playback = FakePlaybackForSubtitle()
        self.theme_color = object()
        self.pause_calls = 0
        self.suspend_calls = 0
        self.resume_calls = 0

    def pause(self):
        self.pause_calls += 1
        self.playback._is_playing = False

    def suspend_for_subtitle_generation(self):
        self.suspend_calls += 1

    def resume_after_subtitle_generation(self):
        self.resume_calls += 1


class FakeSubtitleWorker:
    def __init__(self):
        self.cancel_calls = 0
        self.force_stop_calls = 0

    def cancel(self):
        self.cancel_calls += 1

    def force_stop(self):
        self.force_stop_calls += 1


class FakeSubtitleService(QObject):
    shutdown_finished = Signal()

    def __init__(self):
        super().__init__()
        self.shutdown_in_progress = False
        self.begin_shutdown_result = False
        self.begin_force_shutdown_result = False
        self.begin_shutdown_calls = 0
        self.begin_force_shutdown_calls = 0

    def has_active_tasks(self) -> bool:
        return self.shutdown_in_progress

    def begin_shutdown(self) -> bool:
        self.begin_shutdown_calls += 1
        self.shutdown_in_progress = self.begin_shutdown_result
        return self.begin_shutdown_result

    def begin_force_shutdown(self) -> bool:
        self.begin_force_shutdown_calls += 1
        self.shutdown_in_progress = self.begin_force_shutdown_result
        return self.begin_force_shutdown_result

    def is_shutdown_in_progress(self) -> bool:
        return self.shutdown_in_progress


class FakeMediaStore:
    def __init__(self):
        self.saved_last_open_dir = []
        self.recent_paths = []
        self.saved_positions = []
        self.cleared_positions = []
        self.saved_position_lookup = {}
        self.last_open_dir = ""
        self.shutdown_calls = 0

    def save_last_open_dir(self, path: str):
        self.saved_last_open_dir.append(path)

    def add_recent_path(self, path: str):
        self.recent_paths.append(path)

    def save_position(self, path: str, position_ms: int, total_ms: int):
        self.saved_positions.append((path, position_ms, total_ms))

    def clear_saved_position(self, path: str):
        self.cleared_positions.append(path)

    def get_saved_position(self, path: str) -> int:
        return self.saved_position_lookup.get(path, 0)

    def get_last_open_dir(self) -> str:
        return self.last_open_dir

    def get_recent_media(self):
        return list(self.recent_paths)

    def clear_recent_media(self):
        self.recent_paths.clear()

    def shutdown(self):
        self.shutdown_calls += 1


class FakePlaybackShutdown:
    def __init__(self):
        self.shutdown_calls = 0

    def shutdown(self):
        self.shutdown_calls += 1


class FakeCloseTarget(QWidget):
    def __init__(self):
        super().__init__()
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        return True
