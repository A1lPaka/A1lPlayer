from __future__ import annotations

from PySide6.QtWidgets import QMainWindow

from ui.PiPWindow import PiPWindow
from ui.PlayerWindow import PlayerWindow
from models.ThemeColor import ThemeState
from utils import Metrics


class PiPController:
    def __init__(
        self,
        host_window: QMainWindow,
        player_window: PlayerWindow,
        *,
        metrics: Metrics | None = None,
        theme_color: ThemeState | None = None,
    ):
        self._host_window = host_window
        self._player_window = player_window
        self._metrics = metrics
        self._theme_color = theme_color
        self._pip_window: PiPWindow | None = None

    def is_active(self) -> bool:
        return self._player_window.is_pip_active()

    def apply_metrics(self, metrics: Metrics):
        self._metrics = metrics
        if self._pip_window is not None:
            self._pip_window.apply_metrics(metrics)

    def apply_theme(self, theme_color: ThemeState):
        self._theme_color = theme_color
        if self._pip_window is not None:
            self._pip_window.apply_theme(theme_color)

    def toggle_pip(self):
        if self.is_active():
            self.exit_pip()
            return
        self.enter_pip()

    def enter_pip(self):
        if self.is_active() or not self._player_window.can_activate_view_modes():
            return

        if self._host_window.isFullScreen():
            self._host_window.showNormal()
            self._host_window.sync_fullscreen_ui()

        pip_window = self._ensure_pip_window()
        player_widget = self._host_window.take_player_window()
        if player_widget is None:
            return

        pip_window.setCentralWidget(player_widget)
        self._player_window.set_pip_active(True)
        self.update_aspect_ratio()
        pip_window.show()
        pip_window.raise_()
        pip_window.activateWindow()
        self._player_window.bind_video_output()
        self._host_window.hide()

    def exit_pip(self):
        if not self.is_active():
            return

        pip_window = self._ensure_pip_window()
        player_widget = pip_window.takeCentralWidget()
        if player_widget is None:
            return

        self._host_window.restore_player_window(player_widget)
        self._player_window.set_pip_active(False)
        pip_window.hide()
        self._host_window.showNormal()
        self._host_window.raise_()
        self._host_window.activateWindow()
        self._player_window.bind_video_output()

    def toggle_fullscreen_window(self) -> bool:
        if not self.is_active():
            return False

        pip_window = self._ensure_pip_window()
        if pip_window.isFullScreen():
            pip_window.showNormal()
        else:
            pip_window.showFullScreen()
        return True

    def update_aspect_ratio(self, width: int | None = None, height: int | None = None):
        pip_window = self._pip_window
        if pip_window is None:
            return

        if width is None or height is None:
            geometry = self._player_window.get_video_dimensions()
            if geometry is None:
                return
            width, height = geometry

        pip_window.set_video_aspect_ratio(width, height)

    def _ensure_pip_window(self) -> PiPWindow:
        if self._pip_window is None:
            self._pip_window = PiPWindow(self._metrics, self._theme_color)
            self._pip_window.setWindowIcon(self._host_window.windowIcon())
            self._pip_window.closed.connect(self.exit_pip)
        return self._pip_window
