import logging
from enum import Enum, auto
from typing import Callable

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QDragEnterEvent, QDropEvent

from services.MediaDialogs import MediaDialogs
from services.MediaPathService import MediaPathService
from services.MediaSettingsStore import MediaSettingsStore
from ui.PlayerWindow import PlayerWindow


logger = logging.getLogger(__name__)


class SubtitleAttachResult(Enum):
    LOADED = auto()
    CONTEXT_CHANGED = auto()
    LOAD_FAILED = auto()


ConfirmResumePlayback = Callable[[object, str, int], bool]
ShowMediaAccessFailed = Callable[[object, str | None], None]
ShowOpenSubtitleFailed = Callable[[object], None]


def _decline_resume_playback(_parent, _path: str, _position_ms: int) -> bool:
    return False


def _ignore_media_access_failed(_parent, _path: str | None) -> None:
    return None


def _ignore_open_subtitle_failed(_parent) -> None:
    return None


class MediaLibraryService(QObject):
    _SESSION_AUTOSAVE_INTERVAL_MS = 60_000

    def __init__(
        self,
        parent,
        player_window: PlayerWindow,
        store: MediaSettingsStore,
        confirm_resume_playback: ConfirmResumePlayback | None = None,
        show_media_access_failed: ShowMediaAccessFailed | None = None,
        show_open_subtitle_failed: ShowOpenSubtitleFailed | None = None,
    ):
        super().__init__(parent)
        self._player = player_window
        self._dialogs = MediaDialogs(parent)
        self._paths = MediaPathService()
        self._store = store
        self._confirm_resume_playback = confirm_resume_playback or _decline_resume_playback
        self._show_media_access_failed = show_media_access_failed or _ignore_media_access_failed
        self._show_open_subtitle_failed = show_open_subtitle_failed or _ignore_open_subtitle_failed
        self._pending_recent_request_id: int | None = None
        self._pending_recent_paths: list[str] = []
        self._last_saved_snapshot_key: tuple[str, int, int] | None = None

        self._session_autosave_timer = QTimer(self)
        self._session_autosave_timer.setInterval(self._SESSION_AUTOSAVE_INTERVAL_MS)
        self._session_autosave_timer.timeout.connect(self._autosave_time_session)

        self._player.playback.media_confirmed.connect(self._commit_pending_recent_media)
        self._player.playback.playback_error.connect(self._discard_pending_recent_media)
        self._player.playback.media_confirmed.connect(self._on_media_confirmed)
        self._player.playback.media_finished.connect(self._on_media_finished)
        self._player.playback.playback_error.connect(self._on_playback_error)
        self._player.playback.playback_state_changed.connect(self._on_playback_state_changed)
        self._player.playback.pause_requested.connect(self._save_time_session_on_pause)

    def save_time_session(self):
        snapshot = self._get_valid_session_snapshot()
        if snapshot is None:
            return
        if self._is_duplicate_snapshot(snapshot):
            return

        logger.info(
            "Saving playback session snapshot | media=%s | position_ms=%s | total_ms=%s",
            snapshot["path"],
            snapshot["position_ms"],
            snapshot["total_ms"],
        )
        self._store.save_position(
            snapshot["path"],
            int(snapshot["position_ms"]),
            int(snapshot["total_ms"]),
        )
        self._last_saved_snapshot_key = self._build_snapshot_key(snapshot)

    def shutdown(self):
        self._session_autosave_timer.stop()
        self.save_time_session()

    def open_file(self):
        file_paths = self._dialogs.choose_media_files(self._store.get_last_open_dir())
        if file_paths:
            self.open_media_paths(file_paths)

    def open_folder(self):
        folder_path = self._dialogs.choose_media_folder(self._store.get_last_open_dir())
        if not folder_path:
            return

        self._store.save_last_open_dir(folder_path)
        logger.info("Opening media folder | folder=%s", folder_path)
        try:
            media_paths = self._paths.collect_media_files(folder_path)
        except OSError:
            logger.exception("Failed to open media folder | folder=%s", folder_path)
            self._show_media_access_failed(self._player, folder_path)
            return
        self.open_media_paths(media_paths)

    def open_recent_media(self, path: str) -> bool:
        logger.info("Opening recent media item | media=%s", path)
        return self.open_media_paths([path])

    def open_subtitle(self) -> bool:
        subtitle_path = self._dialogs.choose_subtitle_file(self._store.get_last_open_dir())
        if not subtitle_path:
            return False

        return self.attach_subtitle(
            subtitle_path,
            source="manual",
            save_last_dir=True,
            show_failure_ui=True,
        ) == SubtitleAttachResult.LOADED

    def open_media_paths(self, file_paths: list[str]) -> bool:
        normalized_paths = self._paths.deduplicate_paths(file_paths)
        if not normalized_paths:
            logger.info("Open media request ignored because no usable media paths were provided")
            self._show_media_access_failed(self._player, None)
            return False

        logger.info("Opening media paths | count=%s | first=%s", len(normalized_paths), normalized_paths[0])
        self.save_time_session()

        start_position_ms = 0
        if len(normalized_paths) == 1:
            start_position_ms = self._resolve_start_position_ms(normalized_paths[0])

        if not self._player.playback.open_paths(
            normalized_paths,
            start_position_ms=start_position_ms,
        ):
            logger.error(
                "Playback controller rejected media open request | count=%s | first=%s",
                len(normalized_paths),
                normalized_paths[0],
            )
            self._pending_recent_request_id = None
            self._pending_recent_paths = []
            self._show_media_access_failed(self._player, normalized_paths[0])
            return False

        self._pending_recent_request_id = self._player.playback.current_request_id()
        self._pending_recent_paths = list(normalized_paths)
        logger.info(
            "Media open request started | request_id=%s | pending_recent_count=%s",
            self._pending_recent_request_id,
            len(self._pending_recent_paths),
        )
        return True

    def can_accept_drag_event(self, event: QDragEnterEvent) -> bool:
        if not event.mimeData().hasUrls():
            return False
        urls = event.mimeData().urls()
        if not self._paths.are_local_file_urls(urls):
            return False
        return self._can_apply_drop_data(
            self._paths.cheap_classify_drag_paths(self._paths.urls_to_local_paths(urls))
        )

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
        try:
            drop_data = self._paths.classify_drop_paths(dropped_paths)
        except OSError:
            logger.exception("Failed to open dropped paths")
            self._show_media_access_failed(self._player, dropped_paths[0] if dropped_paths else None)
            return False
        media_paths = drop_data["media_paths"]
        subtitle_paths = drop_data["subtitle_paths"]
        logger.info(
            "Handling dropped paths | raw_count=%s | media_count=%s | subtitle_count=%s",
            len(dropped_paths),
            len(media_paths),
            len(subtitle_paths),
        )

        if media_paths:
            return self.open_media_paths(media_paths)
        if len(subtitle_paths) == 1 and self._player.playback.has_media_loaded():
            return self.attach_subtitle(
                subtitle_paths[0],
                source="drop",
                save_last_dir=True,
                show_failure_ui=True,
            ) == SubtitleAttachResult.LOADED
        return False

    def attach_subtitle(
        self,
        subtitle_path: str,
        *,
        source: str,
        save_last_dir: bool = False,
        guard_media_path: str | None = None,
        guard_request_id: int | None = None,
        show_failure_ui: bool = False,
    ) -> SubtitleAttachResult:
        if save_last_dir:
            self._store.save_last_open_dir(subtitle_path)

        if guard_media_path is not None:
            current_media_path = self._player.playback.current_media_path()
            current_request_id = self._player.playback.current_request_id()
            if current_media_path != guard_media_path or (
                guard_request_id is not None and current_request_id != guard_request_id
            ):
                logger.info(
                    "Skipping subtitle attach because playback context changed | source=%s | subtitle=%s | expected_media=%s | expected_request_id=%s | active_media=%s | active_request_id=%s",
                    source,
                    subtitle_path,
                    guard_media_path,
                    guard_request_id,
                    current_media_path or "<none>",
                    current_request_id,
                )
                return SubtitleAttachResult.CONTEXT_CHANGED

        logger.info(
            "Attaching subtitle to current playback | source=%s | subtitle=%s | media=%s",
            source,
            subtitle_path,
            self._player.playback.current_media_path(),
        )
        if self._player.playback.open_subtitle_file(subtitle_path):
            return SubtitleAttachResult.LOADED

        logger.warning("Subtitle attach failed; current subtitles were preserved | source=%s | subtitle=%s", source, subtitle_path)
        if show_failure_ui:
            self._show_open_subtitle_failed(self._player)
        return SubtitleAttachResult.LOAD_FAILED

    def get_recent_media(self) -> list[str]:
        return self._store.get_recent_media()

    def clear_recent_media(self):
        self._store.clear_recent_media()

    def _resolve_start_position_ms(self, path: str) -> int:
        position_ms = self._store.get_saved_position(path)
        if position_ms <= 0:
            return 0

        if self._confirm_resume_playback(self._player, path, position_ms):
            logger.info("Resuming saved playback position | media=%s | position_ms=%s", path, position_ms)
            return position_ms
        logger.info("User declined saved playback position | media=%s | position_ms=%s", path, position_ms)
        return 0

    def _can_apply_drop_data(self, drop_data: dict[str, list[str]]) -> bool:
        if drop_data["media_paths"]:
            return True
        return (
            len(drop_data["subtitle_paths"]) == 1
            and self._player.playback.has_media_loaded()
        )

    def _commit_pending_recent_media(self, request_id: int, confirmed_path: str):
        if request_id != self._pending_recent_request_id:
            return
        if confirmed_path not in self._pending_recent_paths:
            logger.warning(
                "Confirmed media did not match pending recent-media batch | request_id=%s | media=%s",
                request_id,
                confirmed_path,
            )
            self._pending_recent_request_id = None
            self._pending_recent_paths = []
            return

        self._store.save_last_open_dir(confirmed_path)
        self._store.add_recent_path(confirmed_path)
        logger.info("Committed recent media entry | request_id=%s | media=%s", request_id, confirmed_path)
        self._pending_recent_request_id = None
        self._pending_recent_paths = []

    def _discard_pending_recent_media(self, request_id: int, _failed_path: str, _message: str):
        if request_id != self._pending_recent_request_id:
            return
        logger.info("Discarding pending recent media batch after playback failure | request_id=%s", request_id)
        self._pending_recent_request_id = None
        self._pending_recent_paths = []

    def _autosave_time_session(self):
        self.save_time_session()

    def _save_time_session_on_pause(self):
        self.save_time_session()

    def _on_media_confirmed(self, _request_id: int, _confirmed_path: str):
        self._sync_session_autosave_timer()

    def _on_media_finished(self, path: str):
        logger.info("Clearing saved playback position after media finished | media=%s", path)
        self._store.clear_saved_position(path)
        if self._last_saved_snapshot_key and self._last_saved_snapshot_key[0] == path:
            self._last_saved_snapshot_key = None
        self._session_autosave_timer.stop()

    def _on_playback_error(self, _request_id: int, _path: str, _message: str):
        self._session_autosave_timer.stop()

    def _on_playback_state_changed(self, _state: str):
        self._sync_session_autosave_timer()

    def _sync_session_autosave_timer(self):
        playback = self._player.playback
        should_run = playback.has_media_loaded() and playback.playback_state() != playback.STATE_STOPPED
        if should_run:
            if not self._session_autosave_timer.isActive():
                self._session_autosave_timer.start()
            return
        self._session_autosave_timer.stop()

    def _get_valid_session_snapshot(self) -> dict[str, int | str] | None:
        snapshot = self._player.playback.get_session_snapshot()
        if snapshot is None:
            return None

        path = str(snapshot.get("path") or "")
        position_ms = int(snapshot.get("position_ms") or 0)
        total_ms = int(snapshot.get("total_ms") or 0)
        if not path or position_ms <= 0 or total_ms <= 0:
            return None
        if position_ms > total_ms:
            return None

        return {
            "path": path,
            "position_ms": position_ms,
            "total_ms": total_ms,
        }

    def _is_duplicate_snapshot(self, snapshot: dict[str, int | str]) -> bool:
        return self._build_snapshot_key(snapshot) == self._last_saved_snapshot_key

    def _build_snapshot_key(self, snapshot: dict[str, int | str]) -> tuple[str, int, int]:
        return (
            str(snapshot["path"]),
            int(snapshot["position_ms"]),
            int(snapshot["total_ms"]),
        )
