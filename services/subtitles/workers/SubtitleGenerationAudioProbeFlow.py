import logging
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import QWidget

from services.subtitles.validation.SubtitleGenerationPreflight import AudioStreamProbeState, SubtitleGenerationPreflight
from services.subtitles.workers.SubtitleGenerationWorkers import AudioStreamProbeWorker
from ui.MessageBoxService import show_audio_stream_inspection_warning


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _AudioProbeCacheEntry:
    media_path: str
    state: AudioStreamProbeState
    audio_streams: list | None = None
    error: str | None = None


@dataclass
class _AudioProbeSession:
    request_id: int
    media_path: str
    worker: AudioStreamProbeWorker
    thread: QThread


class SubtitleGenerationAudioProbeFlow(QObject):
    thread_finished = Signal()

    def __init__(
        self,
        parent: QWidget,
        player,
        ui,
        preflight: SubtitleGenerationPreflight,
        *,
        is_generation_dialog_open: Callable[[], bool],
        dialog_media_path: Callable[[], str | None],
        dialog_lifecycle_state_name: Callable[[], str],
    ):
        super().__init__(parent)
        self._parent = parent
        self._player = player
        self._ui = ui
        self._preflight = preflight
        self._is_generation_dialog_open = is_generation_dialog_open
        self._dialog_media_path = dialog_media_path
        self._dialog_lifecycle_state_name = dialog_lifecycle_state_name
        self._cache_entry: _AudioProbeCacheEntry | None = None
        self._next_probe_request_id = 0
        self._active_session_id: int | None = None
        self._sessions: dict[int, _AudioProbeSession] = {}

    @property
    def current_probe_request_id(self) -> int | None:
        if self._active_session_id is None:
            return None
        if self._active_session_id not in self._sessions:
            self._active_session_id = None
            return None
        return self._active_session_id

    @property
    def probe_state(self) -> AudioStreamProbeState:
        if self._cache_entry is None:
            return AudioStreamProbeState.IDLE
        return self._cache_entry.state

    @property
    def cached_audio_streams(self):
        if self._cache_entry is None or self._cache_entry.state != AudioStreamProbeState.READY:
            return None
        return self._cache_entry.audio_streams

    @property
    def workers(self) -> dict[int, AudioStreamProbeWorker]:
        return {request_id: session.worker for request_id, session in self._sessions.items()}

    def load_generation_audio_tracks_async(self, media_path: str):
        cached_audio_streams = self.get_cached_audio_streams_for_media(media_path)
        if cached_audio_streams is not None:
            logger.debug(
                "Using cached audio stream probe result for generation dialog | media=%s | stream_count=%s",
                media_path,
                len(cached_audio_streams),
            )
            self._apply_loaded_audio_tracks(media_path, cached_audio_streams)
            return

        cached_error = self.get_cached_audio_stream_error_for_media(media_path)
        if cached_error is not None:
            logger.debug(
                "Using cached audio stream probe failure for generation dialog | media=%s | reason=%s",
                media_path,
                cached_error,
            )
            self._apply_audio_track_probe_failure(media_path, cached_error, show_warning=False)
            return

        player_audio_track_count = self._get_player_audio_track_count()
        if player_audio_track_count == 1:
            logger.debug(
                "Skipping audio stream probe for generation dialog because player reports a single audio track | media=%s | player_audio_track_count=%s",
                media_path,
                player_audio_track_count,
            )
            self._cache_probe_success(media_path, [])
            self._apply_default_audio_track_only(media_path)
            return

        self._ui.set_generation_dialog_audio_tracks_loading()
        self._begin_probe(media_path)
        self._next_probe_request_id += 1
        probe_request_id = self._next_probe_request_id

        worker = AudioStreamProbeWorker(probe_request_id, media_path)
        thread = QThread(self._parent)
        worker.moveToThread(thread)
        session = _AudioProbeSession(
            request_id=probe_request_id,
            media_path=str(media_path),
            worker=worker,
            thread=thread,
        )
        self._active_session_id = probe_request_id

        thread.started.connect(worker.run)
        worker.finished.connect(self._on_probe_finished, Qt.QueuedConnection)
        worker.failed.connect(self._on_probe_failed, Qt.QueuedConnection)
        worker.canceled.connect(self._on_probe_canceled, Qt.QueuedConnection)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.canceled.connect(thread.quit)
        thread.finished.connect(
            lambda probe_request_id=probe_request_id: self._on_probe_thread_finished(probe_request_id)
        )
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        self._sessions[probe_request_id] = session
        QTimer.singleShot(
            0,
            lambda probe_request_id=probe_request_id: self._deferred_start_probe_worker(
                probe_request_id,
            ),
        )

    def invalidate_active_request(self, reason: str, *, force: bool = False):
        session = self._active_session()
        if session is None:
            return
        self._abandon_loading_probe()
        logger.debug(
            "Invalidating active audio stream probe request | probe_request_id=%s | reason=%s",
            session.request_id,
            reason,
        )
        self._active_session_id = None
        self._request_probe_stop(session.request_id, force=force)

    def is_active(self) -> bool:
        return any(session.thread.isRunning() for session in self._sessions.values())

    def stop_all(self, reason: str, *, force: bool = False):
        if not self._sessions:
            return
        self._abandon_loading_probe()
        self._active_session_id = None
        logger.debug(
            "Stopping all audio stream probe requests | count=%s | reason=%s | force=%s",
            len(self._sessions),
            reason,
            force,
        )
        for probe_request_id in list(self._sessions):
            self._request_probe_stop(probe_request_id, force=force)

    def probe_state_for_media(self, media_path: str | None) -> AudioStreamProbeState:
        normalized_media_path = str(media_path or "")
        if self._cache_entry is None or not normalized_media_path or self._cache_entry.media_path != normalized_media_path:
            return AudioStreamProbeState.IDLE
        return self._cache_entry.state

    def get_cached_audio_streams_for_media(self, media_path: str | None):
        if self.probe_state_for_media(media_path) != AudioStreamProbeState.READY:
            return None
        return self._cache_entry.audio_streams if self._cache_entry is not None else None

    def get_cached_audio_stream_error_for_media(self, media_path: str | None) -> str | None:
        if self.probe_state_for_media(media_path) != AudioStreamProbeState.FAILED:
            return None
        return self._cache_entry.error if self._cache_entry is not None else None

    def _begin_probe(self, media_path: str):
        self._cache_entry = _AudioProbeCacheEntry(
            media_path=str(media_path),
            state=AudioStreamProbeState.LOADING,
        )

    def _abandon_loading_probe(self):
        if self.probe_state != AudioStreamProbeState.LOADING:
            return
        self._cache_entry = None

    def _cache_probe_success(self, media_path: str, audio_streams):
        self._cache_entry = _AudioProbeCacheEntry(
            media_path=str(media_path),
            state=AudioStreamProbeState.READY,
            audio_streams=list(audio_streams),
        )

    def _cache_probe_failure(self, media_path: str, reason: str):
        self._cache_entry = _AudioProbeCacheEntry(
            media_path=str(media_path),
            state=AudioStreamProbeState.FAILED,
            error=str(reason).strip() or "Audio stream inspection failed.",
        )

    def _active_session(self) -> _AudioProbeSession | None:
        if self._active_session_id is None:
            return None
        session = self._sessions.get(self._active_session_id)
        if session is None:
            self._active_session_id = None
        return session

    def _get_player_audio_track_count(self) -> int | None:
        try:
            tracks = self._player.get_audio_tracks()
        except (AttributeError, TypeError, ValueError):
            logger.debug("Player audio track list is unavailable for subtitle generation preflight", exc_info=True)
            return None

        try:
            return sum(1 for track_id, _title in tracks if int(track_id) >= 0)
        except (TypeError, ValueError):
            logger.debug("Player audio track list was malformed for subtitle generation preflight", exc_info=True)
            return None

    def _is_current_probe_result(self, probe_request_id: int, media_path: str) -> bool:
        active_session = self._active_session()
        if active_session is None or active_session.request_id != probe_request_id:
            logger.debug(
                "Ignoring stale audio stream probe result because request ownership changed | probe_request_id=%s | active_probe_request_id=%s | media=%s",
                probe_request_id,
                self._active_session_id,
                media_path,
            )
            return False

        if not self._is_generation_dialog_open():
            logger.debug(
                "Ignoring audio stream probe result because generation dialog is no longer open | probe_request_id=%s | state=%s | media=%s",
                probe_request_id,
                self._dialog_lifecycle_state_name(),
                media_path,
            )
            return False

        if not self._ui.has_generation_dialog():
            logger.debug(
                "Ignoring audio stream probe result because the generation dialog no longer exists | probe_request_id=%s | media=%s",
                probe_request_id,
                media_path,
            )
            return False

        active_media_path = self._dialog_media_path() or self._player.playback.current_media_path()
        if active_media_path != media_path:
            logger.debug(
                "Ignoring stale audio stream probe result because dialog media changed | probe_request_id=%s | result_media=%s | active_media=%s",
                probe_request_id,
                media_path,
                active_media_path,
            )
            return False

        return True

    def _apply_loaded_audio_tracks(self, media_path: str, audio_streams):
        audio_tracks = self._preflight.build_audio_track_choices(audio_streams)
        selector_enabled = bool(audio_streams)
        self._ui.apply_generation_dialog_audio_tracks(
            audio_tracks,
            selected_track_id=None,
            selector_enabled=selector_enabled,
            generate_enabled=True,
        )
        logger.info(
            "Audio stream probe applied to generation dialog | media=%s | stream_count=%s | selector_enabled=%s",
            media_path,
            len(audio_streams),
            selector_enabled,
        )

    def _apply_default_audio_track_only(self, media_path: str):
        self._ui.apply_generation_dialog_audio_tracks(
            self._preflight.build_audio_track_choices([]),
            selected_track_id=None,
            selector_enabled=False,
            generate_enabled=True,
        )
        logger.debug(
            "Generation dialog using default audio track only | media=%s",
            media_path,
        )

    def _apply_audio_track_probe_failure(self, media_path: str, reason: str, *, show_warning: bool):
        formatted_reason = self._preflight.format_audio_stream_probe_error(reason)
        self._ui.apply_generation_dialog_audio_tracks(
            self._preflight.build_audio_track_choices([]),
            selected_track_id=None,
            selector_enabled=False,
            generate_enabled=True,
        )
        if show_warning:
            show_audio_stream_inspection_warning(self._parent, formatted_reason)
        logger.warning(
            "Audio stream probe left generation dialog in fallback state | media=%s | reason=%s",
            media_path,
            formatted_reason,
        )

    @Slot(int, str, object)
    def _on_probe_finished(self, probe_request_id: int, media_path: str, audio_streams):
        try:
            if not self._is_current_probe_result(probe_request_id, media_path):
                return

            self._active_session_id = None
            self._cache_probe_success(media_path, audio_streams)
            self._apply_loaded_audio_tracks(media_path, audio_streams)
        finally:
            self._release_probe_if_thread_stopped(probe_request_id)

    @Slot(int, str, str)
    def _on_probe_failed(self, probe_request_id: int, media_path: str, reason: str):
        try:
            if not self._is_current_probe_result(probe_request_id, media_path):
                return

            self._active_session_id = None
            self._cache_probe_failure(media_path, reason)
            self._apply_audio_track_probe_failure(media_path, reason, show_warning=True)
        finally:
            self._release_probe_if_thread_stopped(probe_request_id)

    @Slot(int)
    def _on_probe_canceled(self, probe_request_id: int):
        logger.debug("Audio stream probe canceled | probe_request_id=%s", probe_request_id)

    def _request_probe_stop(self, probe_request_id: int, *, force: bool):
        session = self._sessions.get(probe_request_id)
        if session is None:
            return
        if session.thread.isRunning():
            if force:
                session.worker.force_stop()
            else:
                session.worker.cancel()
            return

        self._release_probe_session(probe_request_id)

    def _release_probe_if_thread_stopped(self, probe_request_id: int):
        session = self._sessions.get(probe_request_id)
        if session is None or session.thread.isRunning():
            return
        self._release_probe_session(probe_request_id)

    def _release_probe_session(self, probe_request_id: int):
        session = self._sessions.pop(probe_request_id, None)
        if session is None:
            return
        if self._active_session_id == probe_request_id:
            self._active_session_id = None
        session.worker.deleteLater()
        session.thread.deleteLater()

    def _deferred_start_probe_worker(self, probe_request_id: int):
        session = self._sessions.get(probe_request_id)
        if session is None:
            logger.debug(
                "Skipping deferred audio stream probe start for stale request | probe_request_id=%s",
                probe_request_id,
            )
            return
        if session.thread.isRunning():
            logger.debug(
                "Skipping deferred audio stream probe start because thread is already running | probe_request_id=%s",
                probe_request_id,
            )
            return

        session.thread.start()

    @Slot(int)
    def _on_probe_thread_finished(self, probe_request_id: int):
        logger.debug("Audio stream probe thread finished | probe_request_id=%s", probe_request_id)
        self._sessions.pop(probe_request_id, None)
        if self._active_session_id == probe_request_id:
            self._active_session_id = None
        self.thread_finished.emit()
