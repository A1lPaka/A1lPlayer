import sys
from PySide6.QtCore import Qt, QSettings
from PySide6.QtWidgets import QApplication, QMainWindow
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QIcon, QKeySequence, QShortcut

from services.MediaService import MediaService
from controllers.MenuBar import MenuBarController
from ui.PlayerWindow import PlayerWindow
from controllers.PlayerPiPController import PiPController
from ui.ColorThemeDialog import ColorThemeDialog
from utils import res_path, get_metrics, build_window_title
from models.ThemeColor import ThemeState

class MainWindow(QMainWindow):
    _BASE_WINDOW_TITLE = "A1lPlayer"
    _MAX_MEDIA_TITLE_LENGTH = 36

    def __init__(self, settings: QSettings | None = None):
        super().__init__()
        self._update_window_title()
        self.setWindowIcon(QIcon(res_path("assets/logo.ico")))
        self.setObjectName("mainWindow")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAcceptDrops(True)

        self.settings = settings

        self.metrics = get_metrics(self)
        self._screen_connected = False
        self._theme_dialog: ColorThemeDialog | None = None

        self.media_service = MediaService(self, None, self.settings)
        self.theme_state = self.media_service.load_theme()

        self._fullscreen_shortcuts: list[QShortcut] = []
        
        self.player_window = PlayerWindow(self.metrics, self.theme_state)
        self.player_window.open_file_requested.connect(self.open_file)
        self.player_window.media_drop_requested.connect(self._handle_player_drop_event)
        self.player_window.media_finished.connect(self._on_media_finished)
        self.player_window.current_media_changed.connect(self._on_current_media_changed)
        self.player_window.video_geometry_changed.connect(self._on_video_geometry_changed)
        self.player_window.fullscreen_requested.connect(self.toggle_fullscreen)
        self.player_window.pip_requested.connect(self.toggle_pip)
        self.player_window.pip_exit_requested.connect(self.exit_pip)
        self.setCentralWidget(self.player_window)
        self.pip_controller = PiPController(
            self,
            self.player_window,
            metrics=self.metrics,
            theme_color=self.theme_state,
        )

        self.media_service.set_player(self.player_window)
        self.menu_bar_controller = MenuBarController(self, self.metrics, self.theme_state)
        self._init_shortcuts()

        self.resize(self.metrics.window_width, self.metrics.window_height)
        self.setMinimumSize(self.metrics.window_width // 2, self.metrics.window_height // 2)
        self.apply_metrics(self.metrics)

    def apply_metrics(self, metrics):
        self.metrics = metrics
        self.player_window.apply_metrics(metrics)
        self.menu_bar_controller.apply_metrics(metrics)
        self.pip_controller.apply_metrics(metrics)
        if self._theme_dialog is not None:
            self._theme_dialog.apply_metrics(metrics)
        self.updateGeometry()
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self.apply_metrics(get_metrics(self))
        self.sync_fullscreen_ui()

        if not self._screen_connected:
            handle = self.windowHandle()
            if handle:
                handle.screenChanged.connect(self.on_screen_changed)
                self._screen_connected = True

    def closeEvent(self, event):
        if self.pip_controller.is_active():
            self.exit_pip()
        self.media_service.save_time_session()
        super().closeEvent(event)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if self.media_service.handle_drag_enter_event(event):
            return
        super().dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent):
        if self.media_service.handle_drop_event(event):
            return
        super().dropEvent(event)

    def on_screen_changed(self, screen):
        self.apply_metrics(get_metrics(self))

    def _init_shortcuts(self):
        for shortcut_text in ("F11", "Alt+Return", "Ctrl+Alt+Return"):
            shortcut = QShortcut(QKeySequence(shortcut_text), self)
            shortcut.setContext(Qt.WindowShortcut)
            shortcut.activated.connect(self.toggle_fullscreen)
            self._fullscreen_shortcuts.append(shortcut)

        exit_fullscreen_shortcut = QShortcut(QKeySequence("Esc"), self)
        exit_fullscreen_shortcut.setContext(Qt.WindowShortcut)
        exit_fullscreen_shortcut.activated.connect(self.exit_fullscreen)
        self._fullscreen_shortcuts.append(exit_fullscreen_shortcut)

    def toggle_fullscreen(self):
        if self.pip_controller.toggle_fullscreen_window():
            return

        fullscreen = self.is_fullscreen()
        if not fullscreen and not self.player_window.can_activate_view_modes():
            return
        if fullscreen:
            self.showNormal()
        else:
            self.showFullScreen()
        self.sync_fullscreen_ui()

    def exit_fullscreen(self):
        if not self.is_fullscreen():
            return
        self.showNormal()
        self.sync_fullscreen_ui()

    def is_fullscreen(self) -> bool:
        return self.isFullScreen()

    def sync_fullscreen_ui(self):
        fullscreen = self.is_fullscreen()
        menu_bar = self.menuBar()
        if menu_bar is not None:
            menu_bar.setVisible(not fullscreen)
        self.player_window.set_fullscreen_mode(fullscreen)

    def take_player_window(self) -> PlayerWindow | None:
        player_widget = self.takeCentralWidget()
        if isinstance(player_widget, PlayerWindow):
            return player_widget
        if player_widget is not None:
            self.setCentralWidget(player_widget)
        return None

    def restore_player_window(self, player_window: PlayerWindow):
        self.setCentralWidget(player_window)
        self.sync_fullscreen_ui()

    def exit_after_current(self, enabled: bool):
        self.player_window.set_exit_after_current(enabled)

    def is_exit_after_current_enabled(self) -> bool:
        return self.player_window.is_exit_after_current_enabled()

    def open_file(self):
        self.media_service.open_file()

    def open_folder(self):
        self.media_service.open_folder()

    def open_subtitle(self) -> bool:
        return self.media_service.open_subtitle()

    def _handle_player_drop_event(self, event):
        if isinstance(event, QDragEnterEvent):
            self.media_service.handle_drag_enter_event(event)
            return
        if isinstance(event, QDropEvent):
            self.media_service.handle_drop_event(event)

    def _on_media_finished(self, path: str):
        self.media_service.clear_saved_position(path)

    def _on_current_media_changed(self, path: str):
        self._update_window_title(path)
        self.pip_controller.update_aspect_ratio()

    def _on_video_geometry_changed(self, width: int, height: int):
        self.pip_controller.update_aspect_ratio(width, height)

    def _update_window_title(self, media_path: str | None = None):
        self.setWindowTitle(build_window_title(media_path, base_title=self._BASE_WINDOW_TITLE, max_media_title_length=self._MAX_MEDIA_TITLE_LENGTH))

    def get_recent_media(self) -> list[str]:
        return self.media_service.get_recent_media()

    def open_recent_media(self, path: str) -> bool:
        return self.media_service.open_recent_media(path)

    def clear_recent_media(self):
        self.media_service.clear_recent_media()

    def toggle_pip(self):
        self.pip_controller.toggle_pip()

    def open_theme_dialog(self):
        if self._theme_dialog is None:
            self._theme_dialog = ColorThemeDialog(self.theme_state, self.metrics, self)
            self._theme_dialog.themeApplied.connect(self._apply_theme_from_dialog)
            self._theme_dialog.destroyed.connect(self._on_theme_dialog_destroyed)
        elif not self._theme_dialog.isVisible():
            self._theme_dialog.set_theme_color(self.theme_state)

        self._theme_dialog.show()
        self._theme_dialog.raise_()
        self._theme_dialog.activateWindow()

    def _apply_theme_from_dialog(self, theme_color: ThemeState):
        self.theme_state = theme_color
        self.player_window.apply_theme(self.theme_state)
        self.menu_bar_controller.theme_color = self.theme_state
        self.menu_bar_controller.setup_style()
        self.media_service.save_theme(self.theme_state)
        self.pip_controller.apply_theme(self.theme_state)
        if self._theme_dialog is not None:
            self._theme_dialog.close()

    def _on_theme_dialog_destroyed(self):
        self._theme_dialog = None

    def enter_pip(self):
        self.pip_controller.enter_pip()

    def exit_pip(self):
        self.pip_controller.exit_pip()

    def get_audio_tracks(self) -> list[tuple[int, str]]:
        return self.player_window.get_audio_tracks()

    def get_current_audio_track(self) -> int:
        return self.player_window.get_current_audio_track()

    def set_audio_track(self, track_id: int) -> bool:
        return self.player_window.set_audio_track(track_id)

    def get_audio_devices(self) -> list[tuple[str, str]]:
        return self.player_window.get_audio_devices()

    def get_current_audio_device(self) -> str:
        return self.player_window.get_current_audio_device()

    def set_audio_device(self, device_id: str) -> bool:
        return self.player_window.set_audio_device(device_id)

    def get_audio_channel_modes(self) -> list[tuple[str, str]]:
        return self.player_window.get_audio_channel_modes()

    def get_current_audio_channel(self) -> str:
        return self.player_window.get_current_audio_channel()

    def set_audio_channel(self, channel: str) -> bool:
        return self.player_window.set_audio_channel(channel)

    def get_subtitle_tracks(self) -> list[tuple[int, str]]:
        return self.player_window.get_subtitle_tracks()

    def get_current_subtitle_track(self) -> int:
        return self.player_window.get_current_subtitle_track()

    def set_subtitle_track(self, track_id: int) -> bool:
        return self.player_window.set_subtitle_track(track_id)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(res_path("assets/logo.ico")))

    settings = QSettings("Cute_Alpaca_Club", "A1lPlayer")

    window = MainWindow(settings=settings)
    window.show()

    sys.exit(app.exec())
