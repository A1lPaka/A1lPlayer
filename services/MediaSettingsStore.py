import json
import os

from PySide6.QtCore import QSettings

from models.ThemeColor import ThemeState
from utils import normalize_path


class MediaSettingsStore:
    _LAST_OPEN_DIR_KEY = "media/last_open_dir"
    _RECENT_MEDIA_KEY = "media/recent_items"
    _MAX_RECENT_ITEMS = 10
    _MAX_SESSION_ITEMS = _MAX_RECENT_ITEMS
    _SESSION_POSITIONS_KEY = "session/file_positions"
    _THEME_SETTINGS_KEY = "theme/colors"
    _COMPLETION_GRACE_MS = 2000
    _COMPLETION_GRACE_RATIO = 0.98

    def __init__(self, settings: QSettings | None):
        self._settings = settings
        self._session_positions_cache: dict[str, int] | None = None
        self._normalized_session_positions_cache: dict[str, int] | None = None

    def load_theme(self) -> ThemeState:
        if self._settings is None:
            return ThemeState()

        raw = self._settings.value(self._THEME_SETTINGS_KEY, "{}", type=str)
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return ThemeState()

        if not isinstance(data, dict):
            return ThemeState()

        base_colors: dict[str, tuple[int, int, int]] = {}
        for key, value in data.items():
            if key not in ThemeState.DEFAULTS:
                continue
            if not isinstance(value, (list, tuple)) or len(value) != 3:
                continue
            if not all(isinstance(channel, (int, float)) for channel in value):
                continue
            base_colors[key] = tuple(int(channel) for channel in value)

        return ThemeState(base_colors)

    def save_theme(self, theme_color: ThemeState):
        if self._settings is None:
            return

        self._settings.setValue(
            self._THEME_SETTINGS_KEY,
            json.dumps(theme_color.base_colors(), ensure_ascii=True),
        )

    def get_last_open_dir(self) -> str:
        if self._settings is None:
            return ""
        return self._settings.value(self._LAST_OPEN_DIR_KEY, "", type=str)

    def save_last_open_dir(self, file_path: str):
        if self._settings is None:
            return
        if os.path.isdir(file_path):
            self._settings.setValue(self._LAST_OPEN_DIR_KEY, self._storage_path(file_path))
            return
        open_dir = os.path.dirname(file_path)
        self._settings.setValue(
            self._LAST_OPEN_DIR_KEY,
            self._storage_path(open_dir) if open_dir else "",
        )

    def get_saved_position(self, path: str) -> int:
        normalized_path = normalize_path(path)
        return self._get_normalized_session_positions().get(normalized_path, 0)

    def save_position(self, path: str, position_ms: int, total_ms: int):
        if self._settings is None or not path or position_ms <= 0:
            return

        if self.is_media_completed(position_ms, total_ms):
            self.clear_saved_position(path)
            return

        data = self._load_session_positions()
        storage_path = self._storage_path(path)
        data = {k: v for k, v in data.items() if normalize_path(k) != storage_path}
        data[storage_path] = int(position_ms)
        if len(data) > self._MAX_SESSION_ITEMS:
            data = dict(list(data.items())[-self._MAX_SESSION_ITEMS:])

        self._save_session_positions(data)

    def clear_saved_position(self, path: str):
        if self._settings is None or not path:
            return

        data = self._load_session_positions()
        normalized_path = normalize_path(path)
        filtered = {
            saved_path: saved_ms
            for saved_path, saved_ms in data.items()
            if normalize_path(saved_path) != normalized_path
        }
        if filtered != data:
            self._save_session_positions(filtered)

    def get_recent_media(self) -> list[str]:
        return self._get_recent_media_paths()

    def add_recent_path(self, path: str):
        if not path:
            return

        normalized = normalize_path(path)
        paths = [
            item for item in self._get_recent_media_paths()
            if normalize_path(item) != normalized
        ]
        paths.insert(0, self._storage_path(path))
        self._set_recent_media_paths(paths[:self._MAX_RECENT_ITEMS])

    def clear_recent_media(self):
        self._set_recent_media_paths([])

    def is_media_completed(self, position_ms: int, total_ms: int) -> bool:
        if position_ms <= 0 or total_ms <= 0:
            return False

        threshold_ms = min(
            total_ms,
            max(total_ms - self._COMPLETION_GRACE_MS, int(total_ms * self._COMPLETION_GRACE_RATIO)),
        )
        return position_ms >= threshold_ms

    def _load_session_positions(self) -> dict[str, int]:
        if self._session_positions_cache is None:
            self._set_session_positions_cache(self._read_session_positions())
        return dict(self._session_positions_cache)

    def _read_session_positions(self) -> dict[str, int]:
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
                result[self._storage_path(path)] = int(saved_ms)
        return result

    def _save_session_positions(self, data: dict[str, int]):
        if self._settings is None:
            return
        self._settings.setValue(
            self._SESSION_POSITIONS_KEY,
            json.dumps(data, ensure_ascii=True),
        )
        self._set_session_positions_cache(data)

    def _set_session_positions_cache(self, data: dict[str, int]):
        self._session_positions_cache = dict(data)
        normalized: dict[str, int] = {}
        for path, saved_ms in self._session_positions_cache.items():
            normalized.setdefault(normalize_path(path), saved_ms)
        self._normalized_session_positions_cache = normalized

    def _get_normalized_session_positions(self) -> dict[str, int]:
        if self._normalized_session_positions_cache is None:
            self._load_session_positions()
        return self._normalized_session_positions_cache or {}

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

        paths = []
        seen: set[str] = set()
        for entry in data:
            if not isinstance(entry, str) or not entry:
                continue
            storage_path = self._storage_path(entry)
            if storage_path in seen:
                continue
            seen.add(storage_path)
            paths.append(storage_path)
        return paths[:self._MAX_RECENT_ITEMS]

    def _set_recent_media_paths(self, paths: list[str]):
        if self._settings is None:
            return
        storage_paths = []
        seen: set[str] = set()
        for path in paths:
            if not isinstance(path, str) or not path:
                continue
            storage_path = self._storage_path(path)
            if storage_path in seen:
                continue
            seen.add(storage_path)
            storage_paths.append(storage_path)
        self._settings.setValue(
            self._RECENT_MEDIA_KEY,
            json.dumps(storage_paths[:self._MAX_RECENT_ITEMS], ensure_ascii=True),
        )

    def _storage_path(self, path: str) -> str:
        return normalize_path(path)
