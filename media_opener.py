import os
import json

from PySide6.QtCore import QSettings
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QFileDialog, QMessageBox, QWidget

from ThemeColor import ThemeColor
from utils import _format_ms, _normalize_path
from PlayerWindow import PlayerWindow


class MediaOpener:
    _MEDIA_FILTER = "Media Files (*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.webm *.m4v *.mp3 *.wav *.flac *.m4a *.aac);;All Files (*)"
    _SUBTITLE_FILTER = "Subtitle Files (*.srt *.ass *.ssa *.sub *.vtt);;All Files (*)"
    _MEDIA_EXTENSIONS = {
        ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
        ".mp3", ".wav", ".flac", ".m4a", ".aac",
    }
    _SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".sub", ".vtt"}
    _LAST_OPEN_DIR_KEY = "media/last_open_dir"
    _RECENT_MEDIA_KEY = "media/recent_items"
    _MAX_RECENT_ITEMS = 10
    _MAX_SESSION_ITEMS = _MAX_RECENT_ITEMS
    _SESSION_POSITIONS_KEY = "session/file_positions"
    _THEME_SETTINGS_KEY = "theme/colors"
    _COMPLETION_GRACE_MS = 2000
    _COMPLETION_GRACE_RATIO = 0.98

    # ────────────────────────────── init ───────────────────────────────────

    def __init__(self, parent: QWidget, player_controls: PlayerWindow | None, settings: QSettings | None):
        self._parent = parent
        self._player = player_controls
        self._settings = settings

    def set_player(self, player_controls: PlayerWindow):
        self._player = player_controls

    # ─────────────────────────── public methods ───────────────────────────

    def save_time_session(self):
        if self._settings is None:
            return
        if self._player is None:
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

    def load_theme(self) -> ThemeColor:
        if self._settings is None:
            return ThemeColor()

        raw = self._settings.value(self._THEME_SETTINGS_KEY, "{}", type=str)
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return ThemeColor()

        if not isinstance(data, dict):
            return ThemeColor()

        base_colors: dict[str, tuple[int, int, int]] = {}
        for key, value in data.items():
            if key not in ThemeColor.DEFAULTS:
                continue
            if not isinstance(value, (list, tuple)) or len(value) != 3:
                continue
            if not all(isinstance(channel, (int, float)) for channel in value):
                continue
            base_colors[key] = tuple(int(channel) for channel in value)

        return ThemeColor(base_colors)

    def save_theme(self, theme_color: ThemeColor):
        if self._settings is None:
            return

        self._settings.setValue(
            self._THEME_SETTINGS_KEY,
            json.dumps(theme_color.base_colors(), ensure_ascii=True),
        )

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

        self.open_media_paths(file_paths)

    def open_folder(self):
        folder_path = QFileDialog.getExistingDirectory(
            self._parent,
            "Open Media Folder",
            self._last_open_dir(),
        )
        if not folder_path:
            return

        self._save_last_open_dir(folder_path)
        self.open_media_paths(self._collect_media_files(folder_path))

    def open_recent_media(self, path: str) -> bool:
        if not os.path.isfile(path):
            return False

        return self.open_media_paths([path])

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

    def open_media_paths(self, file_paths: list[str]) -> bool:
        normalized_paths = self._deduplicate_paths(file_paths)
        if not normalized_paths:
            return False

        self.save_time_session()

        start_position_ms = 0
        if len(normalized_paths) == 1:
            start_position_ms = self._resolve_start_position_ms(normalized_paths[0])

        if not self._player.open_paths(normalized_paths, start_position_ms=start_position_ms):
            return False

        self._save_last_open_dir(normalized_paths[0])
        for path in reversed(normalized_paths):
            self._add_recent_path(path)
        return True

    def can_accept_drag_event(self, event: QDragEnterEvent) -> bool:
        if not event.mimeData().hasUrls():
            return False
        drop_data = self._classify_drop_paths(self._urls_to_local_paths(event.mimeData().urls()))
        return self._can_apply_drop_data(drop_data)

    def handle_drag_enter_event(self, event: QDragEnterEvent) -> bool:
        if not self.can_accept_drag_event(event):
            event.ignore()
            return False

        event.acceptProposedAction()
        return True

    def handle_drop_event(self, event: QDropEvent) -> bool:
        paths = self._urls_to_local_paths(event.mimeData().urls())
        if not paths:
            event.ignore()
            return False

        if not self.open_dropped_paths(paths):
            event.ignore()
            return False

        event.acceptProposedAction()
        return True

    def open_dropped_paths(self, dropped_paths: list[str]) -> bool:
        drop_data = self._classify_drop_paths(dropped_paths)
        media_paths = drop_data["media_paths"]
        subtitle_paths = drop_data["subtitle_paths"]

        if media_paths:
            return self.open_media_paths(media_paths)
        if len(subtitle_paths) == 1 and self._player is not None and self._player.has_media_loaded():
            subtitle_path = subtitle_paths[0]
            self._save_last_open_dir(subtitle_path)
            return self._player.open_subtitle_file(subtitle_path)
        return False

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

    def _classify_drop_paths(self, dropped_paths: list[str]) -> dict[str, list[str]]:
        media_paths: list[str] = []
        subtitle_paths: list[str] = []

        for path in self._deduplicate_paths(dropped_paths):
            if os.path.isdir(path):
                media_paths.extend(self._collect_media_files(path))
                continue
            if not os.path.isfile(path):
                continue

            _, ext = os.path.splitext(path)
            ext = ext.lower()
            if ext in self._MEDIA_EXTENSIONS:
                media_paths.append(path)
            elif ext in self._SUBTITLE_EXTENSIONS:
                subtitle_paths.append(path)

        media_paths = self._deduplicate_paths(media_paths)
        subtitle_paths = self._deduplicate_paths(subtitle_paths)
        return {
            "media_paths": media_paths,
            "subtitle_paths": subtitle_paths,
        }

    def _can_apply_drop_data(self, drop_data: dict[str, list[str]]) -> bool:
        if drop_data["media_paths"]:
            return True
        return (
            len(drop_data["subtitle_paths"]) == 1
            and self._player is not None
            and self._player.has_media_loaded()
        )

    def _deduplicate_paths(self, paths: list[str]) -> list[str]:
        unique_paths: list[str] = []
        seen: set[str] = set()
        for path in paths:
            normalized = _normalize_path(path)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            unique_paths.append(path)
        return unique_paths

    def _urls_to_local_paths(self, urls) -> list[str]:
        local_paths: list[str] = []
        for url in urls:
            if not url.isLocalFile():
                continue
            local_path = url.toLocalFile()
            if local_path:
                local_paths.append(local_path)
        return local_paths

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
