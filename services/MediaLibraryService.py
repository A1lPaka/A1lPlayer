import os

from PySide6.QtGui import QDragEnterEvent, QDropEvent

from services.MediaDialogs import MediaDialogs
from services.MediaPathService import MediaPathService
from services.MediaSettingsStore import MediaSettingsStore
from ui.PlayerWindow import PlayerWindow


class MediaLibraryService:
    def __init__(
        self,
        parent,
        player_window: PlayerWindow,
        store: MediaSettingsStore,
    ):
        self._player = player_window
        self._dialogs = MediaDialogs(parent)
        self._paths = MediaPathService()
        self._store = store

    def save_time_session(self):
        snapshot = self._player.playback.get_session_snapshot()
        if snapshot is None:
            return

        self._store.save_position(
            snapshot["path"],
            int(snapshot["position_ms"]),
            int(snapshot["total_ms"]),
        )

    def clear_saved_position(self, path: str):
        self._store.clear_saved_position(path)

    def open_file(self):
        file_paths = self._dialogs.choose_media_files(self._store.get_last_open_dir())
        if file_paths:
            self.open_media_paths(file_paths)

    def open_folder(self):
        folder_path = self._dialogs.choose_media_folder(self._store.get_last_open_dir())
        if not folder_path:
            return

        self._store.save_last_open_dir(folder_path)
        self.open_media_paths(self._paths.collect_media_files(folder_path))

    def open_recent_media(self, path: str) -> bool:
        if not os.path.isfile(path):
            return False
        return self.open_media_paths([path])

    def open_subtitle(self) -> bool:
        subtitle_path = self._dialogs.choose_subtitle_file(self._store.get_last_open_dir())
        if not subtitle_path:
            return False

        self._store.save_last_open_dir(subtitle_path)
        return self._player.playback.open_subtitle_file(subtitle_path)

    def open_media_paths(self, file_paths: list[str]) -> bool:
        normalized_paths = self._paths.deduplicate_paths(file_paths)
        if not normalized_paths:
            return False

        self.save_time_session()

        start_position_ms = 0
        if len(normalized_paths) == 1:
            start_position_ms = self._resolve_start_position_ms(normalized_paths[0])

        if not self._player.playback.open_paths(
            normalized_paths,
            start_position_ms=start_position_ms,
        ):
            return False

        self._store.save_last_open_dir(normalized_paths[0])
        for path in reversed(normalized_paths):
            self._store.add_recent_path(path)
        return True

    def can_accept_drag_event(self, event: QDragEnterEvent) -> bool:
        if not event.mimeData().hasUrls():
            return False
        drop_data = self._paths.classify_drop_paths(
            self._paths.urls_to_local_paths(event.mimeData().urls())
        )
        return self._can_apply_drop_data(drop_data)

    def handle_drag_enter_event(self, event: QDragEnterEvent) -> bool:
        if not self.can_accept_drag_event(event):
            event.ignore()
            return False

        event.acceptProposedAction()
        return True

    def handle_drop_event(self, event: QDropEvent) -> bool:
        paths = self._paths.urls_to_local_paths(event.mimeData().urls())
        if not paths:
            event.ignore()
            return False

        if not self.open_dropped_paths(paths):
            event.ignore()
            return False

        event.acceptProposedAction()
        return True

    def open_dropped_paths(self, dropped_paths: list[str]) -> bool:
        drop_data = self._paths.classify_drop_paths(dropped_paths)
        media_paths = drop_data["media_paths"]
        subtitle_paths = drop_data["subtitle_paths"]

        if media_paths:
            return self.open_media_paths(media_paths)
        if len(subtitle_paths) == 1 and self._player.playback.has_media_loaded():
            subtitle_path = subtitle_paths[0]
            self._store.save_last_open_dir(subtitle_path)
            return self._player.playback.open_subtitle_file(subtitle_path)
        return False

    def get_recent_media(self) -> list[str]:
        return self._store.get_recent_media()

    def clear_recent_media(self):
        self._store.clear_recent_media()

    def _resolve_start_position_ms(self, path: str) -> int:
        position_ms = self._store.get_saved_position(path)
        if position_ms <= 0:
            return 0

        if self._dialogs.confirm_resume_playback(path, position_ms):
            return position_ms
        return 0

    def _can_apply_drop_data(self, drop_data: dict[str, list[str]]) -> bool:
        if drop_data["media_paths"]:
            return True
        return (
            len(drop_data["subtitle_paths"]) == 1
            and self._player.playback.has_media_loaded()
        )
