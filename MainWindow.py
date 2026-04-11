import logging
import sys
from functools import partial
from services.runtime.RuntimeInstallerMain import try_run_runtime_installer
from services.runtime.RuntimeHelperMain import try_run_runtime_helper
from PySide6.QtCore import Qt, QSettings
from PySide6.QtWidgets import QApplication, QMainWindow
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QIcon, QKeySequence, QShortcut

from services.AppCloseCoordinator import AppCloseCoordinator
from services.MediaLibraryService import MediaLibraryService
from services.MediaSettingsStore import MediaSettingsStore
from services.subtitles.SubtitleGenerationService import SubtitleGenerationService
from controllers.MenuBar import MenuBarController
from ui.PlayerWindow import PlayerWindow
from controllers.PlayerPiPController import PiPController
from ui.ColorThemeDialog import ColorThemeDialog
from ui.MessageBoxService import show_playback_error
from utils import res_path, get_metrics, build_window_title
from utils.LoggingSetup import configure_logging
from models.ThemeColor import ThemeState


logger = logging.getLogger(__name__)


_runtime_installer_exit_code = try_run_runtime_installer(sys.argv[1:])
if _runtime_installer_exit_code is not None:
    raise SystemExit(_runtime_installer_exit_code)

_runtime_helper_exit_code = try_run_runtime_helper(sys.argv[1:])
if _runtime_helper_exit_code is not None:
    raise SystemExit(_runtime_helper_exit_code)


class MainWindow(QMainWindow):
    _BASE_WINDOW_TITLE = "A1lPlayer"
    _MAX_MEDIA_TITLE_LENGTH = 36
    _SPEED_STEP = 0.25

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
        self._chrome_hidden = False

        self.media_store = MediaSettingsStore(self.settings)
        self.theme_state = self.media_store.load_theme()

        self._window_shortcuts: list[QShortcut] = []
        self._pip_shortcuts: list[QShortcut] = []
        
        self.player_window = PlayerWindow(self.metrics, theme_color=self.theme_state)
        self.media_library = MediaLibraryService(self, self.player_window, self.media_store)
        self.subtitle_service = SubtitleGenerationService(self, self.player_window, self.media_store)
        self.player_window.open_file_requested.connect(self.media_library.open_file)
        self.player_window.media_drop_requested.connect(self._handle_player_drop_event)
        self.player_window.media_finished.connect(self._on_media_finished)
        self.player_window.active_media_changed.connect(self._on_active_media_changed)
        self.player_window.playback_error.connect(self._on_playback_error)
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

        self.menu_bar_controller = MenuBarController(
            main_window=self,
            player_window=self.player_window,
            media_library=self.media_library,
            subtitle_service=self.subtitle_service,
            metrics=self.metrics,
            theme_color=self.theme_state,
        )
        self.app_close_coordinator = AppCloseCoordinator(
            self,
            self.subtitle_service,
            self.media_library,
            is_pip_active=self.pip_controller.is_active,
            exit_pip=self.exit_pip,
        )
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
        close_result = self.app_close_coordinator.attempt_close()
        if not close_result.can_close:
            event.ignore()
            return
        event.accept()
        super().closeEvent(event)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if self.media_library.handle_drag_enter_event(event):
            return
        super().dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent):
        if self.media_library.handle_drop_event(event):
            return
        super().dropEvent(event)

    def on_screen_changed(self, screen):
        self.apply_metrics(get_metrics(self))

    def _init_shortcuts(self):
        self._register_shortcuts(self, self._window_shortcuts, self._build_shortcut_bindings(self.exit_fullscreen))

    def _build_shortcut_bindings(self, escape_handler, *, include_fullscreen: bool = True):
        bindings = [
            ("F11", self.toggle_fullscreen),
            ("Alt+Return", self.toggle_fullscreen),
            ("Ctrl+Alt+Return", self.toggle_fullscreen),
            ("Esc", escape_handler),
            ("Space", self.player_window.player_actions.on_play_pause),
            ("Up", partial(self.player_window.adjust_volume, 10)),
            ("Down", partial(self.player_window.adjust_volume, -10)),
            ("Left", partial(self.player_window.seek_by_ms, -10_000)),
            ("Right", partial(self.player_window.seek_by_ms, 10_000)),
            ("Shift+Left", partial(self.player_window.seek_by_ms, -5_000)),
            ("Shift+Right", partial(self.player_window.seek_by_ms, 5_000)),
            ("Ctrl+Left", partial(self.player_window.seek_by_ms, -60_000)),
            ("Ctrl+Right", partial(self.player_window.seek_by_ms, 60_000)),
            ("+", partial(self.player_window.adjust_speed, self._SPEED_STEP)),
            ("-", partial(self.player_window.adjust_speed, -self._SPEED_STEP)),
            ("=", self.player_window.reset_speed),
            ("Ctrl+H", self.toggle_chrome_visibility),
        ]
        if not include_fullscreen:
            bindings = [binding for binding in bindings if binding[0] not in {"F11", "Alt+Return", "Ctrl+Alt+Return"}]
        return tuple(bindings)

    def init_pip_shortcuts(self, pip_window):
        if self._pip_shortcuts:
            return
        self._register_shortcuts(
            pip_window,
            self._pip_shortcuts,
            self._build_shortcut_bindings(self.exit_pip, include_fullscreen=False),
        )

    def _register_shortcuts(self, parent, storage: list[QShortcut], shortcut_bindings):
        for shortcut_text, handler in shortcut_bindings:
            self._register_window_shortcut(parent, storage, shortcut_text, handler)

    def _register_window_shortcut(self, parent, storage: list[QShortcut], shortcut_text: str, handler):
        shortcut = QShortcut(QKeySequence(shortcut_text), parent)
        shortcut.setContext(Qt.WindowShortcut)
        shortcut.activated.connect(handler)
        storage.append(shortcut)

    def toggle_fullscreen(self):
        if self.pip_controller.toggle_fullscreen_window():
            return

        fullscreen = self.is_fullscreen()
        if not fullscreen and not self.player_window.playback.can_activate_view_modes():
            return
        if fullscreen:
            self.showNormal()
        else:
            self.showFullScreen()
        self.sync_fullscreen_ui()

    def exit_fullscreen(self):
        if self.pip_controller.is_active():
            self.exit_pip()
            return
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
            menu_bar.setVisible(not fullscreen and not self._chrome_hidden)
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

    def _handle_player_drop_event(self, event):
        if isinstance(event, QDragEnterEvent):
            self.media_library.handle_drag_enter_event(event)
            return
        if isinstance(event, QDropEvent):
            self.media_library.handle_drop_event(event)

    def _on_media_finished(self, path: str):
        self.media_library.clear_saved_position(path)

    def _on_active_media_changed(self, path: str | None):
        self._update_window_title(path)
        self.pip_controller.update_aspect_ratio()

    def _on_video_geometry_changed(self, width: int, height: int):
        self.pip_controller.update_aspect_ratio(width, height)

    def _on_playback_error(self, path: str, message: str):
        logger.warning("Playback error surfaced to UI | media=%s | message=%s", path or "<unknown>", message)
        show_playback_error(self, message, path)

    def _update_window_title(self, media_path: str | None = None):
        self.setWindowTitle(build_window_title(media_path, base_title=self._BASE_WINDOW_TITLE, max_media_title_length=self._MAX_MEDIA_TITLE_LENGTH))

    def toggle_pip(self):
        self.pip_controller.toggle_pip()

    def toggle_chrome_visibility(self):
        self._chrome_hidden = not self._chrome_hidden
        self.player_window.set_chrome_hidden(self._chrome_hidden)
        self.sync_fullscreen_ui()

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
        self.menu_bar_controller.apply_theme(self.theme_state)
        self.media_store.save_theme(self.theme_state)
        self.pip_controller.apply_theme(self.theme_state)
        if self._theme_dialog is not None:
            self._theme_dialog.close()

    def _on_theme_dialog_destroyed(self):
        self._theme_dialog = None

    def enter_pip(self):
        self.pip_controller.enter_pip()

    def exit_pip(self):
        self.pip_controller.exit_pip()

def main(argv: list[str] | None = None) -> int:
    app_argv = list(sys.argv if argv is None else argv)
    log_file_path = configure_logging()
    logger.info("Application startup initiated%s", f" | log_file={log_file_path}" if log_file_path else "")
    app = QApplication(app_argv)
    app.setWindowIcon(QIcon(res_path("assets/logo.ico")))

    settings = QSettings("Cute_Alpaca_Club", "A1lPlayer")

    window = MainWindow(settings=settings)
    window.show()

    exit_code = app.exec()
    logger.info("Application event loop finished | exit_code=%s", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
