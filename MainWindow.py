import sys
from PySide6.QtCore import Qt, QSettings
from PySide6.QtWidgets import QApplication, QMainWindow
from PySide6.QtGui import QIcon

from media_opener import MediaOpener
from MenuBar import MenuBarConfigurator
from PlayerWindow import PlayerWindow
from utils import res_path, get_metrics
from ThemeColor import ThemeColor

class MainWindow(QMainWindow):
    def __init__(self, settings: QSettings | None = None):
        super().__init__()
        self.setWindowTitle("A1lPlayer")
        self.setWindowIcon(QIcon(res_path("assets/logo.ico")))
        self.setObjectName("mainWindow")
        self.setAttribute(Qt.WA_StyledBackground, True)

        self.settings = settings

        self.metrics = get_metrics(self)
        self._screen_connected = False

        self.theme_color = ThemeColor()

        # Создаём и добавляем PlayerControls
        self.player_controls = PlayerWindow(self.metrics, self.theme_color)
        self.player_controls.open_file_requested.connect(self.open_file)
        self.player_controls.media_finished.connect(self._on_media_finished)
        self.setCentralWidget(self.player_controls)

        self.media_opener = MediaOpener(self, self.player_controls, self.settings)
        self.menu_bar_config = MenuBarConfigurator(self, self.metrics, self.theme_color)

        self.resize(self.metrics.window_width, self.metrics.window_height)
        self.apply_metrics(self.metrics)

    def apply_metrics(self, metrics):
        self.metrics = metrics
        self.player_controls.apply_metrics(metrics)
        self.menu_bar_config.apply_metrics(metrics)
        self.updateGeometry()
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self.apply_metrics(get_metrics(self))

        if not self._screen_connected:
            handle = self.windowHandle()
            if handle:
                handle.screenChanged.connect(self.on_screen_changed)
                self._screen_connected = True

    def closeEvent(self, event):
        self.media_opener.save_time_session()
        super().closeEvent(event)

    def on_screen_changed(self, screen):
        self.apply_metrics(get_metrics(self))

    def exit_after_current(self, enabled: bool):
        self.player_controls.set_exit_after_current(enabled)

    def is_exit_after_current_enabled(self) -> bool:
        return self.player_controls.is_exit_after_current_enabled()

    def open_file(self):
        self.media_opener.open_file()

    def open_folder(self):
        self.media_opener.open_folder()

    def _on_media_finished(self, path: str):
        self.media_opener.clear_saved_position(path)

    def get_recent_media(self) -> list[str]:
        return self.media_opener.get_recent_media()

    def open_recent_media(self, path: str) -> bool:
        return self.media_opener.open_recent_media(path)

    def clear_recent_media(self):
        self.media_opener.clear_recent_media()

    def get_audio_tracks(self) -> list[tuple[int, str]]:
        return self.player_controls.get_audio_tracks()

    def get_current_audio_track(self) -> int:
        return self.player_controls.get_current_audio_track()

    def set_audio_track(self, track_id: int) -> bool:
        return self.player_controls.set_audio_track(track_id)

    def get_audio_devices(self) -> list[tuple[str, str]]:
        return self.player_controls.get_audio_devices()

    def get_current_audio_device(self) -> str:
        return self.player_controls.get_current_audio_device()

    def set_audio_device(self, device_id: str) -> bool:
        return self.player_controls.set_audio_device(device_id)

    def get_audio_channel_modes(self) -> list[tuple[str, str]]:
        return self.player_controls.get_audio_channel_modes()

    def get_current_audio_channel(self) -> str:
        return self.player_controls.get_current_audio_channel()

    def set_audio_channel(self, channel: str) -> bool:
        return self.player_controls.set_audio_channel(channel)

    def get_subtitle_tracks(self) -> list[tuple[int, str]]:
        return self.player_controls.get_subtitle_tracks()

    def get_current_subtitle_track(self) -> int:
        return self.player_controls.get_current_subtitle_track()

    def set_subtitle_track(self, track_id: int) -> bool:
        return self.player_controls.set_subtitle_track(track_id)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(res_path("assets/logo.ico")))

    settings = QSettings("Cute_Alpaca_Club", "A1lPlayer")

    window = MainWindow(settings=settings)
    window.show()

    sys.exit(app.exec())
