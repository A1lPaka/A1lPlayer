from __future__ import annotations

import logging

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMainWindow

from ui.PiPWindow import PiPWindow
from ui.PlayerWindow import PlayerWindow
from models.ThemeColor import ThemeState
from utils import Metrics


logger = logging.getLogger(__name__)


class ViewModeController:
    _REBIND_FALLBACK_TIMEOUT_MS = 700
    _PLAYBACK_INTERRUPTION_OWNER = "pip_rebind"

    def __init__(
        self,
        host_window: QMainWindow,
        player_window: PlayerWindow,
        *,
        metrics: Metrics | None = None,
        theme_color: ThemeState,
    ):
        self._host_window = host_window
        self._player_window = player_window
        self._metrics = metrics
        self._theme_color = theme_color
        self._pip_window: PiPWindow | None = None
        self._initial_video_output_bound = False
        self._rebind_lease = self._player_window.playback.create_interruption_lease(self._PLAYBACK_INTERRUPTION_OWNER)
        self._pending_transition_id = 0
        self._pending_rebind_bound = False
        self._awaiting_rebind_geometry = False
        self._pending_geometry_slot = None
        self._rebind_fallback_timer = QTimer(self._player_window)
        self._rebind_fallback_timer.setSingleShot(True)
        self._rebind_fallback_timer.setInterval(self._REBIND_FALLBACK_TIMEOUT_MS)
        self._rebind_fallback_timer.timeout.connect(self._on_rebind_fallback_timeout)

        self._player_window.media_finished.connect(self._on_media_finished)
        self._player_window.video_host_ready.connect(self._on_video_host_ready)

    def is_active(self) -> bool:
        return self._player_window.is_pip_active()

    def toggle_pip(self):
        if self.is_active():
            self.exit_pip()
            return
        self.enter_pip()

    def toggle_fullscreen(self):
        if self.is_active():
            self._toggle_pip_fullscreen()
            return

        if self._host_window.isFullScreen():
            self.exit_fullscreen()
            return
        if not self._player_window.playback.can_activate_view_modes():
            return

        self._host_window.showFullScreen()
        self.sync_host_window_ui()

    def exit_fullscreen(self):
        if not self._host_window.isFullScreen():
            return
        self._host_window.showNormal()
        self.sync_host_window_ui()

    def apply_metrics(self, metrics: Metrics):
        self._metrics = metrics
        if self._pip_window is not None:
            self._pip_window.apply_metrics(metrics)

    def apply_theme(self, theme_color: ThemeState):
        self._theme_color = theme_color
        if self._pip_window is not None:
            self._pip_window.apply_theme(theme_color)

    def enter_pip(self):
        if self.is_active() or not self._player_window.playback.can_activate_view_modes():
            return

        self._rebind_lease.acquire()

        self.exit_fullscreen()

        pip_window = self._ensure_pip_window()
        player_widget = self._host_window._take_player_window_for_view_mode()
        if player_widget is None:
            self._rebind_lease.release()
            return

        pip_window.setCentralWidget(player_widget)
        self._player_window.set_pip_active(True)
        self.update_aspect_ratio()
        pip_window.show()
        pip_window.raise_()
        pip_window.activateWindow()
        self._host_window.hide()
        self._start_rebind_video_output_transition()

    def exit_pip(self):
        if not self.is_active():
            return

        self._rebind_lease.acquire()

        player_widget = self._take_player_widget_from_pip()
        if player_widget is None:
            self._rebind_lease.release()
            return

        self._host_window._restore_player_window_from_view_mode(player_widget)
        self._host_window.showNormal()
        self.sync_host_window_ui()
        self._host_window.raise_()
        self._host_window.activateWindow()
        self._start_rebind_video_output_transition()

    def teardown_for_shutdown(self):
        if not self.is_active():
            return

        self._cancel_pending_rebind_transition()
        player_widget = self._take_player_widget_from_pip()
        if player_widget is not None:
            self._host_window._restore_player_window_from_view_mode(player_widget)

    def sync_host_window_ui(self):
        fullscreen = self._host_window.isFullScreen()
        menu_bar = self._host_window.menuBar()
        if menu_bar is not None:
            menu_bar.setVisible(not fullscreen and not self._player_window.is_chrome_hidden())
        self._player_window.set_fullscreen_mode(fullscreen)

    def update_aspect_ratio(self, width: int | None = None, height: int | None = None):
        pip_window = self._pip_window
        if pip_window is None:
            return

        if width is None or height is None:
            geometry = self._player_window.playback.get_video_dimensions()
            if geometry is None:
                return
            width, height = geometry

        pip_window.set_video_aspect_ratio(width, height)

    def _ensure_pip_window(self) -> PiPWindow:
        if self._pip_window is None:
            self._pip_window = PiPWindow(self._metrics, self._theme_color)
            self._pip_window.setWindowIcon(self._host_window.windowIcon())
            self._host_window.init_pip_shortcuts(self._pip_window)
            self._pip_window.closed.connect(self.exit_pip)
        return self._pip_window

    def _take_player_widget_from_pip(self) -> PlayerWindow | None:
        pip_window = self._ensure_pip_window()
        player_widget = pip_window.takeCentralWidget()
        self._player_window.set_pip_active(False)
        pip_window.hide()
        return player_widget

    def _toggle_pip_fullscreen(self):
        pip_window = self._ensure_pip_window()
        if pip_window.isFullScreen():
            pip_window.showNormal()
            return
        pip_window.showFullScreen()

    def _start_rebind_video_output_transition(self):
        self._cancel_pending_rebind_transition(release_lease=False)
        self._pending_transition_id += 1
        self._pending_rebind_bound = False
        self._awaiting_rebind_geometry = self._rebind_lease.paused_playback
        if self._awaiting_rebind_geometry:
            transition_id = self._pending_transition_id

            def _geometry_slot(width: int, height: int):
                self._on_video_geometry_changed(transition_id, width, height)

            self._pending_geometry_slot = _geometry_slot
            self._player_window.video_geometry_changed.connect(_geometry_slot)
        self._rebind_fallback_timer.start()
        self._try_bind_pending_video_output()

    def _cancel_pending_rebind_transition(self, *, release_lease: bool = True):
        if self._pending_geometry_slot is not None:
            try:
                self._player_window.video_geometry_changed.disconnect(self._pending_geometry_slot)
            except (RuntimeError, TypeError):
                pass
            self._pending_geometry_slot = None
        if release_lease:
            self._rebind_lease.release(resume_playback=False)
        self._pending_rebind_bound = False
        self._awaiting_rebind_geometry = False
        self._rebind_fallback_timer.stop()

    def _has_pending_rebind_transition(self) -> bool:
        return self._pending_rebind_bound or self._awaiting_rebind_geometry or self._rebind_fallback_timer.isActive()

    def _try_bind_initial_video_output(self):
        if self._initial_video_output_bound:
            return
        if not self._player_window.is_video_host_ready():
            return

        self._player_window.bind_video_output()
        self._initial_video_output_bound = True

    def _try_bind_pending_video_output(self):
        if not self._has_pending_rebind_transition():
            return
        if self._pending_rebind_bound:
            return
        if not self._player_window.is_video_host_ready():
            return

        self._player_window.bind_video_output()
        self._pending_rebind_bound = True
        logger.info(
            "PiP rebind bind completed | transition_id=%s | awaiting_geometry=%s | resume_playback=%s",
            self._pending_transition_id,
            self._awaiting_rebind_geometry,
            self._rebind_lease.paused_playback,
        )

        if not self._rebind_lease.paused_playback:
            self._complete_pending_rebind_transition()
            return

    def _resume_after_successful_rebind(self, transition_id: int):
        if transition_id != self._pending_transition_id:
            return
        if not self._pending_rebind_bound or not self._awaiting_rebind_geometry:
            return

        logger.info(
            "PiP rebind resume via geometry | transition_id=%s",
            transition_id,
        )
        self._complete_pending_rebind_transition(transition_id)

    def _resume_after_rebind_fallback(self, transition_id: int):
        if transition_id != self._pending_transition_id:
            return
        if not self._has_pending_rebind_transition():
            return

        geometry_missing = self._awaiting_rebind_geometry
        if geometry_missing:
            logger.warning(
                "PiP rebind fallback timeout without geometry | transition_id=%s | bound=%s",
                transition_id,
                self._pending_rebind_bound,
            )

        if not self._pending_rebind_bound and self._player_window.is_video_host_ready():
            self._player_window.bind_video_output()
            self._pending_rebind_bound = True
            logger.info(
                "PiP rebind fallback bind completed | transition_id=%s",
                transition_id,
            )

        if self._rebind_lease.paused_playback:
            logger.warning(
                "PiP rebind resume via fallback | transition_id=%s | geometry_missing=%s",
                transition_id,
                geometry_missing,
            )

        self._complete_pending_rebind_transition(transition_id)

    def _complete_pending_rebind_transition(self, transition_id: int | None = None):
        if transition_id is not None and transition_id != self._pending_transition_id:
            return
        self._rebind_lease.release()
        self._cancel_pending_rebind_transition()

    def _on_video_host_ready(self):
        self._try_bind_initial_video_output()
        self._try_bind_pending_video_output()

    def _on_video_geometry_changed(self, transition_id: int, width: int, height: int):
        if transition_id != self._pending_transition_id:
            return
        if width <= 0 or height <= 0:
            return
        if not self._pending_rebind_bound or not self._awaiting_rebind_geometry:
            return
        self._resume_after_successful_rebind(transition_id)

    def _on_rebind_fallback_timeout(self):
        transition_id = self._pending_transition_id
        self._resume_after_rebind_fallback(transition_id)

    def _on_media_finished(self, _path: str):
        if self.is_active():
            self.exit_pip()
