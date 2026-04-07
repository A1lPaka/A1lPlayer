from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtGui import QActionGroup
from PySide6.QtWidgets import QMainWindow
from models.ThemeColor import ThemeState
from utils import Metrics

if TYPE_CHECKING:
    from services.MediaService import MediaService
    from ui.PlayerWindow import PlayerWindow

class MenuBarController:
    def __init__(
        self,
        main_window: QMainWindow,
        player_window: PlayerWindow,
        media_service: MediaService,
        metrics: Metrics | None = None,
        theme_color: ThemeState | None = None,
    ):
        self.main_window = main_window
        self.player_window = player_window
        self.media_service = media_service
        self.metrics = metrics
        self.theme_color = theme_color
        self.setup()
        self.setup_style()

    def setup(self):
        self.menu_bar = self.main_window.menuBar()
        
        # Media
        media_menu = self.menu_bar.addMenu("Media")

        self.open_action = media_menu.addAction("Open File...")
        self.open_action.setShortcut("Ctrl+O")
        self.open_action.triggered.connect(self._on_open_file)

        self.open_folder_action = media_menu.addAction("Open Folder")
        self.open_folder_action.setShortcut("Ctrl+Shift+O")
        self.open_folder_action.triggered.connect(self._on_open_folder)

        self.open_recent_action = media_menu.addMenu("Open Recent Media")
        self.open_recent_action.aboutToShow.connect(self._rebuild_recent_menu)

        media_menu.addSeparator()

        self.exit_after_action = media_menu.addAction("Exit After Current")
        self.exit_after_action.setCheckable(True)
        self.exit_after_action.setChecked(self.player_window.is_exit_after_current_enabled())
        self.exit_after_action.toggled.connect(self._on_exit_after_current)

        self.exit_action = media_menu.addAction("Exit")
        self.exit_action.triggered.connect(self._on_exit)

        # Audio
        audio_menu = self.menu_bar.addMenu("Audio")

        self.audio_track_menu = audio_menu.addMenu("Audio Track")
        self.audio_track_menu.aboutToShow.connect(self._rebuild_audio_track_menu)

        self.audio_device_menu = audio_menu.addMenu("Audio Device")
        self.audio_device_menu.aboutToShow.connect(self._rebuild_audio_device_menu)

        self.stereo_mode_menu = audio_menu.addMenu("Stereo Mode")
        self._init_stereo_mode_menu()

        # Subtitles
        subtitles_menu = self.menu_bar.addMenu("Subtitles")

        self.open_subtitle_action = subtitles_menu.addAction("Open Subtitle")
        self.open_subtitle_action.triggered.connect(self._on_open_subtitle)

        self.generate_subtitle_action = subtitles_menu.addAction("Generate Subtitle") # using OpenAI Whisper

        self.subtitle_track_menu = subtitles_menu.addMenu("Subtitle Track")
        self.subtitle_track_menu.aboutToShow.connect(self._rebuild_subtitle_track_menu)

        # View
        self.theme_action = self.menu_bar.addAction("Theme")
        self.theme_action.triggered.connect(self._on_open_theme_dialog)

    def _rgb(self, name: str) -> str:
        r, g, b = self.theme_color.get(name)
        return f"rgb({r}, {g}, {b})"

    def setup_style(self):
        font = self.metrics.font_size
        width = self.metrics.menu_width
        bg_color = self._rgb("panel_bg_color")
        hovered_color = self._rgb("panel_bg_color_hovered")
        pressed_color = self._rgb("panel_bg_color_pressed")
        separator_color = self._rgb("panel_bg_color_separator")
        text_color = self._rgb("text_color")
        style = f"""
            QMenuBar {{
                background-color: {bg_color};
                color: {text_color};
                font-size: {font}px;
            }}
            QMenuBar::item {{
                background-color: {bg_color};
                color: {text_color};
                font-size: {font}px;
            }}
            QMenuBar::item:selected {{
                background-color: {hovered_color};
            }}
            QMenuBar::item:pressed {{
                background-color: {pressed_color};
            }}
            QMenu {{
                background-color: {bg_color};
                border-top: 1px solid {separator_color};
                padding-top: 1px;
                color: {text_color};
                font-size: {font}px;
                min-width: {width}px;
            }}
            QMenu::item:selected {{
                background-color: {hovered_color};
            }}
            QMenu::item:pressed {{
                background-color: {pressed_color};
            }}
            QMenu::separator {{
                height: 1px;
                background-color: {separator_color};
                margin: 4px 0;
            }}
        """
        self.menu_bar.setStyleSheet(style)

    def apply_metrics(self, metrics: Metrics):
        self.metrics = metrics
        self.setup_style()

    def apply_theme(self, theme_color: ThemeState):
        self.theme_color = theme_color
        self.setup_style()


    def _on_open_file(self):
        self.media_service.open_file()

    def _on_open_folder(self):
        self.media_service.open_folder()

    def _on_open_subtitle(self):
        self.media_service.open_subtitle()

    def _rebuild_recent_menu(self):
        self.open_recent_action.clear()
        items = self.media_service.get_recent_media()

        if not items:
            empty_action = self.open_recent_action.addAction("No recent media")
            empty_action.setEnabled(False)
        else:
            for path in items:
                action = self.open_recent_action.addAction(path)
                action.triggered.connect(
                    lambda checked=False, p=path: self._on_open_recent_item(p)
                )

            self.open_recent_action.addSeparator()

        self.clear_recent_action = self.open_recent_action.addAction("Clear")
        self.clear_recent_action.triggered.connect(self._on_clear_recent)

    def _rebuild_audio_track_menu(self):
        self.audio_track_menu.clear()
        self.audio_track_group = QActionGroup(self.audio_track_menu)
        self.audio_track_group.setExclusive(True)
        tracks = self.player_window.get_audio_tracks()

        if not tracks:
            empty_action = self.audio_track_menu.addAction("No audio tracks")
            empty_action.setEnabled(False)
            return

        current_track = self.player_window.get_current_audio_track()

        for track_id, title in tracks:
            action = self.audio_track_menu.addAction(title)
            action.setCheckable(True)
            action.setChecked(track_id == current_track)
            action.triggered.connect(
                lambda checked=False, tid=track_id: self._on_select_audio_track(tid)
            )
            self.audio_track_group.addAction(action)

    def _rebuild_audio_device_menu(self):
        self.audio_device_menu.clear()
        self.audio_device_group = QActionGroup(self.audio_device_menu)
        self.audio_device_group.setExclusive(True)
        devices = self.player_window.get_audio_devices()

        if not devices:
            empty_action = self.audio_device_menu.addAction("No audio devices")
            empty_action.setEnabled(False)
            return

        current_device = self.player_window.get_current_audio_device()

        for device_id, title in devices:
            action = self.audio_device_menu.addAction(title)
            action.setCheckable(True)
            action.setChecked(device_id == current_device)
            action.triggered.connect(
                lambda checked=False, did=device_id: self._on_select_audio_device(did)
            )
            self.audio_device_group.addAction(action)

    def _rebuild_subtitle_track_menu(self):
        self.subtitle_track_menu.clear()
        self.subtitle_track_group = QActionGroup(self.subtitle_track_menu)
        self.subtitle_track_group.setExclusive(True)
        tracks = self.player_window.get_subtitle_tracks()

        if not tracks:
            empty_action = self.subtitle_track_menu.addAction("No subtitle tracks")
            empty_action.setEnabled(False)
            return

        current_track = self.player_window.get_current_subtitle_track()

        for track_id, title in tracks:
            action = self.subtitle_track_menu.addAction(title)
            action.setCheckable(True)
            action.setChecked(track_id == current_track)
            action.triggered.connect(
                lambda checked=False, tid=track_id: self._on_select_subtitle_track(tid)
            )
            self.subtitle_track_group.addAction(action)

    def _init_stereo_mode_menu(self):
        self.stereo_mode_group = QActionGroup(self.stereo_mode_menu)
        self.stereo_mode_group.setExclusive(True)
        self.stereo_mode_actions: dict[str, object] = {}

        current_channel = self.player_window.get_current_audio_channel()

        for channel_id, title in self.player_window.get_audio_channel_modes():
            action = self.stereo_mode_menu.addAction(title)
            action.setCheckable(True)
            action.setChecked(channel_id == current_channel)
            action.triggered.connect(
                lambda checked=False, cid=channel_id: self._on_select_audio_channel(cid)
            )
            self.stereo_mode_group.addAction(action)
            self.stereo_mode_actions[channel_id] = action

    def _on_open_recent_item(self, path: str):
        self.media_service.open_recent_media(path)

    def _on_select_audio_track(self, track_id: int):
        self.player_window.set_audio_track(track_id)

    def _on_select_audio_device(self, device_id: str):
        self.player_window.set_audio_device(device_id)

    def _on_select_subtitle_track(self, track_id: int):
        self.player_window.set_subtitle_track(track_id)

    def _on_select_audio_channel(self, channel: str):
        if self.player_window.set_audio_channel(channel):
            action = self.stereo_mode_actions.get(channel)
            if action is not None:
                action.setChecked(True)

    def _on_clear_recent(self):
        self.media_service.clear_recent_media()

    def _on_exit_after_current(self, checked: bool):
        self.player_window.set_exit_after_current(checked)

    def _on_exit(self):
        self.main_window.close()

    def _on_open_theme_dialog(self):
        self.main_window.open_theme_dialog()
