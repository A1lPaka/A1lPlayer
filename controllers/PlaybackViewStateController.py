from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal

from controllers.PlayerPlaybackController import PlayerPlaybackController


@dataclass(frozen=True)
class PlaybackViewState:
    phase: str
    media_assigned: bool
    media_confirmed_loaded: bool
    placeholder_visible: bool
    progress_seekable: bool
    position_timer_active: bool
    play_pause_shows_playing: bool


class PlaybackViewStateController(QObject):
    view_state_changed = Signal(object)
    playback_error = Signal(str, str)

    def __init__(self, playback: PlayerPlaybackController, parent: QObject | None = None):
        super().__init__(parent)
        self._playback = playback
        self._current_view_state = self._build_view_state()

        self._playback.playback_state_changed.connect(self._on_playback_state_changed)
        self._playback.playback_error.connect(self._on_playback_error)

    def current_view_state(self) -> PlaybackViewState:
        return self._current_view_state

    def sync(self, force: bool = False):
        self._emit_view_state(force=force)

    def _on_playback_state_changed(self, _state: str):
        self._emit_view_state()

    def _on_playback_error(self, _request_id: int, path: str, message: str):
        self._emit_view_state(force=True)
        self.playback_error.emit(path, message)

    def _emit_view_state(self, force: bool = False):
        next_view_state = self._build_view_state()
        if not force and next_view_state == self._current_view_state:
            return
        self._current_view_state = next_view_state
        self.view_state_changed.emit(next_view_state)

    def _build_view_state(self) -> PlaybackViewState:
        phase = self._playback.playback_state()
        media_assigned = self._playback.has_assigned_media()
        media_confirmed_loaded = self._playback.has_media_loaded()

        if phase == PlayerPlaybackController.STATE_OPENING:
            return PlaybackViewState(
                phase=phase,
                media_assigned=media_assigned,
                media_confirmed_loaded=media_confirmed_loaded,
                placeholder_visible=True,
                progress_seekable=False,
                position_timer_active=False,
                play_pause_shows_playing=False,
            )

        if phase == PlayerPlaybackController.STATE_PLAYING:
            return PlaybackViewState(
                phase=phase,
                media_assigned=media_assigned,
                media_confirmed_loaded=media_confirmed_loaded,
                placeholder_visible=False,
                progress_seekable=True,
                position_timer_active=True,
                play_pause_shows_playing=True,
            )

        if phase == PlayerPlaybackController.STATE_PAUSED:
            return PlaybackViewState(
                phase=phase,
                media_assigned=media_assigned,
                media_confirmed_loaded=media_confirmed_loaded,
                placeholder_visible=False,
                progress_seekable=True,
                position_timer_active=True,
                play_pause_shows_playing=False,
            )

        return PlaybackViewState(
            phase=phase,
            media_assigned=media_assigned,
            media_confirmed_loaded=media_confirmed_loaded,
            placeholder_visible=True,
            progress_seekable=False,
            position_timer_active=False,
            play_pause_shows_playing=False,
        )
