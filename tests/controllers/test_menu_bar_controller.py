from types import SimpleNamespace

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMainWindow

from controllers.MenuBar import MenuBarController
from models.ThemeColor import ThemeState


class _PlaybackStub(QObject):
    active_media_changed = Signal(object)

    def __init__(self):
        super().__init__()

    def has_media_loaded(self) -> bool:
        return False

    def get_current_audio_mode(self) -> str:
        return ""


class _PlayerWindowStub(QObject):
    def __init__(self):
        super().__init__()
        self.playback = _PlaybackStub()

    def get_audio_channel_modes(self):
        return []


class _MediaLibraryStub:
    def __init__(self, recent_paths: list[str]):
        self._recent_paths = list(recent_paths)
        self.open_recent_calls = []
        self.clear_calls = 0

    def open_file(self):
        pass

    def open_folder(self):
        pass

    def open_subtitle(self):
        pass

    def get_recent_media(self) -> list[str]:
        return list(self._recent_paths)

    def open_recent_media(self, path: str):
        self.open_recent_calls.append(path)

    def clear_recent_media(self):
        self.clear_calls += 1


class _SubtitleServiceStub:
    def generate_subtitle(self):
        pass


class _MainWindowStub(QMainWindow):
    def __init__(self):
        super().__init__()
        self.exit_after_current = False

    def is_exit_after_current_enabled(self) -> bool:
        return self.exit_after_current

    def set_exit_after_current(self, checked: bool):
        self.exit_after_current = bool(checked)


def test_recent_menu_rebuilds_from_raw_history_without_validation():
    missing_path = r"\\server\share\offline\movie.mkv"
    main_window = _MainWindowStub()
    controller = MenuBarController(
        main_window=main_window,
        player_window=_PlayerWindowStub(),
        media_library=_MediaLibraryStub([missing_path]),
        subtitle_service=_SubtitleServiceStub(),
        metrics=SimpleNamespace(font_size=12, menu_width=240),
        theme_color=ThemeState(),
    )

    controller._rebuild_recent_menu()

    action_texts = [action.text() for action in controller.open_recent_action.actions()]

    assert action_texts == [missing_path, "", "Clear"]
