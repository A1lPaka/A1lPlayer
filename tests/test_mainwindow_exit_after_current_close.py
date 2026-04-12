import importlib
import sys
import types

from PySide6.QtCore import QObject, QSettings, Signal
from PySide6.QtWidgets import QApplication, QWidget

from services.AppCloseCoordinator import AppCloseResult


class _PlaybackStub:
    def __init__(self):
        self.view_modes_allowed = False

    def is_exit_after_current_enabled(self):
        return False

    def has_media_loaded(self):
        return False

    def can_activate_view_modes(self):
        return self.view_modes_allowed

    def shutdown(self):
        return None


class _PlayerActionsStub:
    def on_play_pause(self):
        return None


class _PlayerWindowStub(QWidget):
    open_file_requested = Signal()
    media_finished = Signal(str)
    active_media_changed = Signal(object)
    playback_error = Signal(str, str)
    video_geometry_changed = Signal(int, int)
    fullscreen_requested = Signal()
    pip_requested = Signal()
    pip_exit_requested = Signal()
    close_requested_after_media_end = Signal()

    def __init__(self, metrics, theme_color):
        super().__init__()
        self.metrics = metrics
        self.theme_color = theme_color
        self.playback = _PlaybackStub()
        self.player_actions = _PlayerActionsStub()

    def apply_metrics(self, metrics):
        self.metrics = metrics

    def apply_theme(self, theme_color):
        self.theme_color = theme_color

    def set_fullscreen_mode(self, _fullscreen: bool):
        return None

    def set_chrome_hidden(self, _hidden: bool):
        return None

    def adjust_volume(self, _delta_percent: int):
        return None

    def seek_by_ms(self, _delta_ms: int):
        return None

    def adjust_speed(self, _delta: float):
        return None

    def reset_speed(self):
        return None


class _MediaSettingsStoreStub:
    def __init__(self, _settings):
        self.theme = object()

    def load_theme(self):
        return self.theme

    def save_theme(self, theme):
        self.theme = theme


class _MediaLibraryServiceStub:
    def __init__(self, _main_window, _player_window, _media_store):
        self.shutdown_calls = 0

    def open_file(self):
        return None

    def shutdown(self):
        self.shutdown_calls += 1


class _SubtitleGenerationServiceStub(QObject):
    shutdown_finished = Signal()

    def __init__(self, _main_window, _player_window, _media_store, _media_library):
        super().__init__()

    def has_active_tasks(self):
        return False

    def begin_shutdown(self):
        return False

    def is_shutdown_in_progress(self):
        return False


class _MenuBarControllerStub:
    def __init__(self, **_kwargs):
        return None

    def apply_metrics(self, _metrics):
        return None

    def apply_theme(self, _theme_color):
        return None


class _ViewModeControllerStub:
    def __init__(self, _main_window, player_window, metrics, theme_color):
        self.player_window = player_window
        self.metrics = metrics
        self.theme_color = theme_color
        self.active = False
        self.exit_calls = 0
        self.exit_fullscreen_calls = 0
        self.shutdown_teardown_calls = 0
        self.enter_calls = 0
        self.toggle_fullscreen_calls = 0
        self.sync_host_window_ui_calls = 0

    def is_active(self):
        return self.active

    def exit_pip(self):
        self.exit_calls += 1
        self.active = False
        return None

    def teardown_for_shutdown(self):
        self.shutdown_teardown_calls += 1
        self.active = False
        return None

    def toggle_fullscreen(self):
        if self.player_window.playback.can_activate_view_modes():
            self.toggle_fullscreen_calls += 1
        return None

    def exit_fullscreen(self):
        self.exit_fullscreen_calls += 1
        return None

    def sync_host_window_ui(self):
        self.sync_host_window_ui_calls += 1
        return None

    def apply_metrics(self, metrics):
        self.metrics = metrics

    def update_aspect_ratio(self, *_args):
        return None

    def enter_pip(self):
        if self.active or not self.player_window.playback.can_activate_view_modes():
            return None
        self.enter_calls += 1
        return None

    def toggle_pip(self):
        if self.active:
            return self.exit_pip()
        return self.enter_pip()

    def apply_theme(self, theme_color):
        self.theme_color = theme_color


def test_exit_after_current_uses_mainwindow_close_flow(monkeypatch):
    installer_module = types.ModuleType("services.runtime.RuntimeInstallerMain")
    helper_module = types.ModuleType("services.runtime.RuntimeHelperMain")
    installer_module.try_run_runtime_installer = lambda argv=None: None
    helper_module.try_run_runtime_helper = lambda argv=None: None
    monkeypatch.setitem(sys.modules, "services.runtime.RuntimeInstallerMain", installer_module)
    monkeypatch.setitem(sys.modules, "services.runtime.RuntimeHelperMain", helper_module)
    message_box_module = sys.modules.get("ui.MessageBoxService")
    if message_box_module is not None and not hasattr(message_box_module, "show_playback_error"):
        monkeypatch.setattr(message_box_module, "show_playback_error", lambda *_args, **_kwargs: None, raising=False)
    sys.modules.pop("MainWindow", None)
    module = importlib.import_module("MainWindow")

    monkeypatch.setattr(module, "PlayerWindow", _PlayerWindowStub)
    monkeypatch.setattr(module, "MediaSettingsStore", _MediaSettingsStoreStub)
    monkeypatch.setattr(module, "MediaLibraryService", _MediaLibraryServiceStub)
    monkeypatch.setattr(module, "SubtitleGenerationService", _SubtitleGenerationServiceStub)
    monkeypatch.setattr(module, "MenuBarController", _MenuBarControllerStub)
    monkeypatch.setattr(module, "ViewModeController", _ViewModeControllerStub)
    monkeypatch.setattr(module, "get_metrics", lambda _window: type("Metrics", (), {"window_width": 1280, "window_height": 720})())
    monkeypatch.setattr(module, "res_path", lambda relative_path: relative_path)

    window = module.MainWindow(settings=QSettings())
    close_attempts = []

    def _attempt_close():
        close_attempts.append(True)
        return AppCloseResult(can_close=False, shutdown_completed=False)

    window.app_close_coordinator.attempt_close = _attempt_close
    window.show()
    QApplication.processEvents()

    window.player_window.close_requested_after_media_end.emit()
    QApplication.processEvents()

    assert close_attempts == [True]

def test_view_modes_are_blocked_in_mainwindow_until_playback_allows_them(monkeypatch):
    installer_module = types.ModuleType("services.runtime.RuntimeInstallerMain")
    helper_module = types.ModuleType("services.runtime.RuntimeHelperMain")
    installer_module.try_run_runtime_installer = lambda argv=None: None
    helper_module.try_run_runtime_helper = lambda argv=None: None
    monkeypatch.setitem(sys.modules, "services.runtime.RuntimeInstallerMain", installer_module)
    monkeypatch.setitem(sys.modules, "services.runtime.RuntimeHelperMain", helper_module)
    message_box_module = sys.modules.get("ui.MessageBoxService")
    if message_box_module is not None and not hasattr(message_box_module, "show_playback_error"):
        monkeypatch.setattr(message_box_module, "show_playback_error", lambda *_args, **_kwargs: None, raising=False)
    sys.modules.pop("MainWindow", None)
    module = importlib.import_module("MainWindow")

    monkeypatch.setattr(module, "PlayerWindow", _PlayerWindowStub)
    monkeypatch.setattr(module, "MediaSettingsStore", _MediaSettingsStoreStub)
    monkeypatch.setattr(module, "MediaLibraryService", _MediaLibraryServiceStub)
    monkeypatch.setattr(module, "SubtitleGenerationService", _SubtitleGenerationServiceStub)
    monkeypatch.setattr(module, "MenuBarController", _MenuBarControllerStub)
    monkeypatch.setattr(module, "ViewModeController", _ViewModeControllerStub)
    monkeypatch.setattr(module, "get_metrics", lambda _window: type("Metrics", (), {"window_width": 1280, "window_height": 720})())
    monkeypatch.setattr(module, "res_path", lambda relative_path: relative_path)

    window = module.MainWindow(settings=QSettings())

    window.player_window.fullscreen_requested.emit()
    window.player_window.pip_requested.emit()
    window.player_window.pip_requested.emit()

    assert window.view_mode_controller.toggle_fullscreen_calls == 0
    assert window.view_mode_controller.enter_calls == 0

    window.player_window.playback.view_modes_allowed = True

    window.player_window.fullscreen_requested.emit()
    window.player_window.pip_requested.emit()
    window.player_window.pip_requested.emit()

    assert window.view_mode_controller.toggle_fullscreen_calls == 1
    assert window.view_mode_controller.enter_calls == 2
