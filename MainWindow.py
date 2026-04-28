import logging
import os
import sys
from functools import partial
from services.runtime.RuntimeInstallerMain import try_run_runtime_installer
from services.runtime.RuntimeHelperMain import try_run_runtime_helper
from PySide6.QtCore import Qt, QSettings, QTimer
from PySide6.QtWidgets import QApplication, QMainWindow
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QIcon, QKeySequence, QShortcut

from services.app.AppCloseCoordinator import AppCloseCoordinator
from services.media.MediaLibraryService import MediaLibraryService
from services.media.MediaPathService import MEDIA_EXTENSIONS
from services.app.MediaSettingsStore import MediaSettingsStore
from services.subtitles.facade.SubtitleGenerationService import SubtitleGenerationService
from controllers.MenuBar import MenuBarController
from ui.PlayerWindow import PlayerWindow
from controllers.ViewModeController import ViewModeController
from ui.ColorThemeDialog import ColorThemeDialog
from ui.MessageBoxService import (
    confirm_resume_playback,
    show_media_access_failed,
    show_no_supported_media_found,
    show_open_subtitle_failed,
    show_playback_error,
)
from utils import res_path, get_metrics, build_window_title
from utils.LoggingSetup import configure_logging
from models.ThemeColor import ThemeState


logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    _BASE_WINDOW_TITLE = "A1lPlayer"
    _MAX_MEDIA_TITLE_LENGTH = 36
    _SPEED_STEP = 0.25

    def __init__(self, settings: QSettings | None = None):
        super().__init__()
        self._init_window()
        self._init_state(settings)
        self._init_components()
        self._wire_components()
        self._finalize_window_setup()

    def _init_window(self):
        self._update_window_title()
        self.setWindowIcon(QIcon(res_path("assets/logo.ico")))
        self.setObjectName("mainWindow")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAcceptDrops(True)

    def _init_state(self, settings: QSettings | None):
        self.settings = settings
        self.metrics = get_metrics(self)
        self._screen_connected = False
        self._theme_dialog: ColorThemeDialog | None = None
        self._chrome_hidden = False
        self._window_shortcuts: list[QShortcut] = []
        self._pip_shortcuts: list[QShortcut] = []
        self._pip_shortcut_parent = None
        self._exit_after_current = False

        self.media_store = MediaSettingsStore(self.settings)
        self.theme_state = self.media_store.load_theme()

    def _init_components(self):
        self.player_window = PlayerWindow(self.metrics, theme_color=self.theme_state)
        self.setCentralWidget(self.player_window)

        self.media_library = MediaLibraryService(
            self,
            self.player_window,
            self.media_store,
            confirm_resume_playback=confirm_resume_playback,
            show_media_access_failed=show_media_access_failed,
            show_no_supported_media_found=show_no_supported_media_found,
            show_open_subtitle_failed=show_open_subtitle_failed,
        )
        self.subtitle_service = SubtitleGenerationService(self, self.player_window, self.media_store, self.media_library)
        self.view_mode_controller = ViewModeController(
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
            shutdown_playback=self.player_window.playback.shutdown,
            is_pip_active=self.view_mode_controller.is_active,
            teardown_pip_for_shutdown=self.view_mode_controller.teardown_for_shutdown,
        )

    def _wire_components(self):
        self.player_window.open_file_requested.connect(self.media_library.open_file)
        self.player_window.playback.media_finished.connect(self._on_media_finished)
        self.player_window.playback.active_media_changed.connect(self._on_active_media_changed)
        self.player_window.playback_error.connect(self._on_playback_error)
        self.player_window.playback.video_geometry_changed.connect(self._on_video_geometry_changed)
        self.player_window.fullscreen_requested.connect(self.view_mode_controller.toggle_fullscreen)
        self.player_window.pip_requested.connect(self.view_mode_controller.toggle_pip)
        self.player_window.pip_exit_requested.connect(self.view_mode_controller.exit_pip)

        self._init_shortcuts()

    def _finalize_window_setup(self):
        self.resize(self.metrics.window_width, self.metrics.window_height)
        self.setMinimumSize(self.metrics.window_width // 2, self.metrics.window_height // 2)
        self.apply_metrics(self.metrics)

    def apply_metrics(self, metrics):
        self.metrics = metrics
        self.player_window.apply_metrics(metrics)
        self.menu_bar_controller.apply_metrics(metrics)
        self.view_mode_controller.apply_metrics(metrics)
        if self._theme_dialog is not None:
            self._theme_dialog.apply_metrics(metrics)
        self.updateGeometry()
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self.apply_metrics(get_metrics(self))
        self.view_mode_controller.sync_host_window_ui()

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
        self._register_shortcuts(self, self._window_shortcuts, self._build_shortcut_bindings(self.view_mode_controller.exit_fullscreen))

    def _build_shortcut_bindings(self, escape_handler, *, include_fullscreen: bool = True):
        bindings = [
            ("F11", self.view_mode_controller.toggle_fullscreen),
            ("Alt+Return", self.view_mode_controller.toggle_fullscreen),
            ("Ctrl+Alt+Return", self.view_mode_controller.toggle_fullscreen),
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
        if self._pip_shortcuts and self._pip_shortcut_parent is pip_window:
            return
        self._clear_shortcuts(self._pip_shortcuts)
        self._pip_shortcut_parent = pip_window
        self._register_shortcuts(
            pip_window,
            self._pip_shortcuts,
            self._build_shortcut_bindings(self.view_mode_controller.exit_pip, include_fullscreen=False),
        )

    def _register_shortcuts(self, parent, storage: list[QShortcut], shortcut_bindings):
        for shortcut_text, handler in shortcut_bindings:
            self._register_window_shortcut(parent, storage, shortcut_text, handler)

    def _register_window_shortcut(self, parent, storage: list[QShortcut], shortcut_text: str, handler):
        shortcut = QShortcut(QKeySequence(shortcut_text), parent)
        shortcut.setContext(Qt.WindowShortcut)
        shortcut.activated.connect(handler)
        storage.append(shortcut)

    def _clear_shortcuts(self, storage: list[QShortcut]):
        for shortcut in storage:
            shortcut.setEnabled(False)
            shortcut.deleteLater()
        storage.clear()

    def _take_player_window_for_view_mode(self) -> PlayerWindow | None:
        player_widget = self.takeCentralWidget()
        if isinstance(player_widget, PlayerWindow):
            return player_widget
        if player_widget is not None:
            self.setCentralWidget(player_widget)
        return None

    def _restore_player_window_from_view_mode(self, player_window: PlayerWindow):
        self.setCentralWidget(player_window)

    def is_exit_after_current_enabled(self) -> bool:
        return self._exit_after_current

    def set_exit_after_current(self, enabled: bool):
        self._exit_after_current = bool(enabled)

    def _on_active_media_changed(self, path: str | None):
        self._update_window_title(path)
        self.view_mode_controller.update_aspect_ratio()

    def _on_media_finished(self, _path: str):
        if not self._exit_after_current:
            return
        self.player_window.playback.stop()
        self.close()

    def _on_video_geometry_changed(self, width: int, height: int):
        self.view_mode_controller.update_aspect_ratio(width, height)

    def _on_playback_error(self, path: str, message: str):
        logger.warning("Playback error surfaced to UI | media=%s | message=%s", path or "<unknown>", message)
        show_playback_error(self, message, path)

    def _update_window_title(self, media_path: str | None = None):
        self.setWindowTitle(build_window_title(media_path, base_title=self._BASE_WINDOW_TITLE, max_media_title_length=self._MAX_MEDIA_TITLE_LENGTH))

    def toggle_chrome_visibility(self):
        self._chrome_hidden = not self._chrome_hidden
        self.player_window.set_chrome_hidden(self._chrome_hidden)
        self.view_mode_controller.sync_host_window_ui()

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
        self.view_mode_controller.apply_theme(self.theme_state)
        if self._theme_dialog is not None:
            self._theme_dialog.close()

    def _on_theme_dialog_destroyed(self):
        self._theme_dialog = None

def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    runtime_exit_code = try_run_runtime_installer(args)
    if runtime_exit_code is not None:
        return runtime_exit_code

    runtime_exit_code = try_run_runtime_helper(args)
    if runtime_exit_code is not None:
        return runtime_exit_code

    app_argv = [sys.argv[0], *args]
    log_file_path = configure_logging()
    logger.info("Application startup initiated%s", f" | log_file={log_file_path}" if log_file_path else "")
    app = QApplication(app_argv)
    app.setWindowIcon(QIcon(res_path("assets/logo.ico")))

    settings = QSettings("Cute_Alpaca_Club", "A1lPlayer")

    window = MainWindow(settings=settings)
    window.show()
    startup_media_paths = _startup_media_paths_from_args(args)
    if startup_media_paths:
        QTimer.singleShot(0, lambda paths=startup_media_paths: window.media_library.open_media_paths(paths))

    exit_code = app.exec()
    logger.info("Application event loop finished | exit_code=%s", exit_code)
    return exit_code


def _startup_media_paths_from_args(args: list[str]) -> list[str]:
    media_paths: list[str] = []
    for raw_arg in args:
        candidate = str(raw_arg or "").strip()
        if not candidate or candidate.startswith("--"):
            continue
        _, extension = os.path.splitext(candidate)
        if extension.lower() not in MEDIA_EXTENSIONS:
            continue
        if os.path.isfile(candidate):
            media_paths.append(candidate)
    return media_paths


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
