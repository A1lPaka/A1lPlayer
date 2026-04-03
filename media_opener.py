import os
import json

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QFileDialog, QMessageBox, QWidget

from utils import _format_ms, _normalize_path


class MediaOpener:
    _MEDIA_FILTER = "Media Files (*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.webm *.m4v *.mp3 *.wav *.flac *.m4a *.aac);;All Files (*)"
    _SUBTITLE_FILTER = "Subtitle Files (*.srt *.ass *.ssa *.sub *.vtt);;All Files (*)"
    _MEDIA_EXTENSIONS = {
        ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
        ".mp3", ".wav", ".flac", ".m4a", ".aac",
    }
    _LAST_OPEN_DIR_KEY = "media/last_open_dir"
    _RECENT_MEDIA_KEY = "media/recent_items"
    _MAX_RECENT_ITEMS = 10
    _MAX_SESSION_ITEMS = _MAX_RECENT_ITEMS
    _SESSION_POSITIONS_KEY = "session/file_positions"
    _COMPLETION_GRACE_MS = 2000
    _COMPLETION_GRACE_RATIO = 0.98

    # ────────────────────────────── init ───────────────────────────────────

    def __init__(self, parent: QWidget, player_controls, settings: QSettings | None):
        self._parent = parent
        self._player = player_controls
        self._settings = settings

    # ─────────────────────────── public methods ───────────────────────────

    def save_time_session(self):
        if self._settings is None:
            return

        snapshot = self._player.get_session_snapshot()
        if snapshot is None:
            return

        current_path = snapshot["path"]
        position_ms = int(snapshot["position_ms"])
        total_ms = int(snapshot["total_ms"])

        if self._is_media_completed(position_ms, total_ms):
            self.clear_saved_position(current_path)
            return
        if position_ms <= 0:
            return

        data = self._load_session_positions()
        normalized_current = _normalize_path(current_path)
        data = {k: v for k, v in data.items() if _normalize_path(k) != normalized_current}
        data[current_path] = int(position_ms)
        if len(data) > self._MAX_SESSION_ITEMS:
            data = dict(list(data.items())[-self._MAX_SESSION_ITEMS:])

        self._save_session_positions(data)

    def clear_saved_position(self, path: str):
        if self._settings is None or not path:
            return

        data = self._load_session_positions()
        normalized_path = _normalize_path(path)
        filtered = {
            saved_path: saved_ms
            for saved_path, saved_ms in data.items()
            if _normalize_path(saved_path) != normalized_path
        }

        if filtered != data:
            self._save_session_positions(filtered)

    def _load_session_positions(self) -> dict[str, int]:
        if self._settings is None:
            return {}

        raw = self._settings.value(self._SESSION_POSITIONS_KEY, "{}", type=str)
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}

        result: dict[str, int] = {}
        for path, saved_ms in data.items():
            if isinstance(path, str) and path and isinstance(saved_ms, (int, float)):
                result[path] = int(saved_ms)
        return result

    def _save_session_positions(self, data: dict[str, int]):
        if self._settings is None:
            return
        self._settings.setValue(
            self._SESSION_POSITIONS_KEY,
            json.dumps(data, ensure_ascii=True),
        )

    def open_file(self):
        file_paths, _ = QFileDialog.getOpenFileNames(
            self._parent,
            "Open Media Files",
            self._last_open_dir(),
            self._MEDIA_FILTER,
        )
        if not file_paths:
            return

        self.save_time_session()

        start_position_ms = 0
        if len(file_paths) == 1:
            start_position_ms = self._resolve_start_position_ms(file_paths[0])

        if self._player.open_paths(file_paths, start_position_ms=start_position_ms):
            self._save_last_open_dir(file_paths[0])
            for path in reversed(file_paths):
                self._add_recent_path(path)

    def open_folder(self):
        folder_path = QFileDialog.getExistingDirectory(
            self._parent,
            "Open Media Folder",
            self._last_open_dir(),
        )
        if not folder_path:
            return

        self._save_last_open_dir(folder_path)
        file_paths = self._collect_media_files(folder_path)
        if not file_paths:
            return

        self.save_time_session()

        start_position_ms = self._resolve_start_position_ms(file_paths[0])
        if self._player.open_paths(file_paths, start_position_ms=start_position_ms):
            for path in reversed(file_paths):
                self._add_recent_path(path)

    def open_recent_media(self, path: str) -> bool:
        if not os.path.isfile(path):
            return False

        self.save_time_session()

        start_position_ms = self._resolve_start_position_ms(path)
        if self._player.open_paths([path], start_position_ms=start_position_ms):
            self._save_last_open_dir(path)
            self._add_recent_path(path)
            return True
        return False

    def open_subtitle(self) -> bool:
        subtitle_path, _ = QFileDialog.getOpenFileName(
            self._parent,
            "Open Subtitle",
            self._last_open_dir(),
            self._SUBTITLE_FILTER,
        )
        if not subtitle_path:
            return False

        self._save_last_open_dir(subtitle_path)
        return self._player.open_subtitle_file(subtitle_path)

    def _resolve_start_position_ms(self, path: str) -> int:
        if self._settings is None:
            return 0

        data = self._load_session_positions()

        normalized_path = _normalize_path(path)
        position_ms = next(
            (saved_ms for saved_path, saved_ms in data.items() if _normalize_path(saved_path) == normalized_path),
            0,
        )
        if position_ms <= 0:
            return 0

        msg = QMessageBox(self._parent)
        msg.setWindowTitle("Resume playback")
        msg.setText(f"Resume playback for:\n{path}\n\nLast position: {_format_ms(position_ms)}\n\nContinue from where you left off?")
        msg.setIcon(QMessageBox.Icon.NoIcon)
        msg.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        return position_ms if msg.exec() == QMessageBox.StandardButton.Yes else 0

    def get_recent_media(self) -> list[str]:
        paths = self._get_recent_media_paths()
        valid = [p for p in paths if os.path.isfile(p)]
        if len(valid) != len(paths):
            self._set_recent_media_paths(valid)
        return valid

    def clear_recent_media(self):
        self._set_recent_media_paths([])

    # ──────────────────────── last directory ──────────────────────────────

    def _last_open_dir(self) -> str:
        if self._settings is None:
            return ""
        return self._settings.value(self._LAST_OPEN_DIR_KEY, "", type=str)

    def _save_last_open_dir(self, file_path: str):
        if self._settings is None:
            return
        if os.path.isdir(file_path):
            self._settings.setValue(self._LAST_OPEN_DIR_KEY, file_path)
            return
        self._settings.setValue(self._LAST_OPEN_DIR_KEY, os.path.dirname(file_path))

    # ────────────────────────── folder scan ───────────────────────────────

    def _collect_media_files(self, folder_path: str) -> list[str]:
        file_paths: list[str] = []
        for entry in os.scandir(folder_path):
            if not entry.is_file():
                continue
            _, ext = os.path.splitext(entry.name)
            if ext.lower() in self._MEDIA_EXTENSIONS:
                file_paths.append(entry.path)

        file_paths.sort(key=lambda p: os.path.basename(p).lower())
        return file_paths

    # ─────────────────────────── recent history ───────────────────────────

    def _add_recent_path(self, path: str):
        if not path:
            return

        normalized = _normalize_path(path)
        paths = self._get_recent_media_paths()
        paths = [
            item for item in paths
            if _normalize_path(item) != normalized
        ]
        paths.insert(0, path)
        self._set_recent_media_paths(paths[:self._MAX_RECENT_ITEMS])

    def _get_recent_media_paths(self) -> list[str]:
        if self._settings is None:
            return []

        raw = self._settings.value(self._RECENT_MEDIA_KEY, "[]", type=str)
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return []

        if not isinstance(data, list):
            return []

        paths = [entry for entry in data if isinstance(entry, str) and entry]

        return paths[:self._MAX_RECENT_ITEMS]

    def _set_recent_media_paths(self, paths: list[str]):
        if self._settings is None:
            return
        self._settings.setValue(
            self._RECENT_MEDIA_KEY,
            json.dumps(paths[:self._MAX_RECENT_ITEMS], ensure_ascii=True),
        )

    def _is_media_completed(self, position_ms: int, total_ms: int) -> bool:
        if position_ms <= 0 or total_ms <= 0:
            return False

        threshold_ms = min(
            total_ms,
            max(total_ms - self._COMPLETION_GRACE_MS, int(total_ms * self._COMPLETION_GRACE_RATIO)),
        )
        return position_ms >= threshold_ms
