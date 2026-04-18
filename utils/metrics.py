from dataclasses import dataclass

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget


BASE_WIDTH = 1920
BASE_HEIGHT = 1080

@dataclass
class Metrics:
    min_window_side: int
    scale_factor: float
    window_width: int
    window_height: int
    icon_size: int
    font_size: int
    menu_width: int
    theme_dialog_width: int
    theme_dialog_height: int
    pip_min_width: int
    subtitle_dialog_width: int
    subtitle_dialog_height: int
    subtitle_progress_dialog_width: int
    subtitle_progress_dialog_height: int


def _build_metrics(min_window_side: int, scale_factor: float) -> Metrics:
    window_width = int(min_window_side * 0.8)
    window_height = int(min_window_side * 0.5)
    icon_size = int(min_window_side / 70 * scale_factor)
    font_size = int(icon_size * 0.7)
    menu_width = int(min_window_side * 0.1)
    theme_dialog_height = int(min_window_side / 1.75)
    theme_dialog_width = min_window_side // 2
    pip_min_width = int(min_window_side / 5.2)
    subtitle_dialog_width = min_window_side // 2
    subtitle_dialog_height = int(min_window_side * 0.3)
    subtitle_progress_dialog_width = int(min_window_side * 0.42)
    subtitle_progress_dialog_height = int(min_window_side * 0.2)

    return Metrics(
        min_window_side,
        scale_factor,
        window_width,
        window_height,
        icon_size,
        font_size,
        menu_width,
        theme_dialog_width,
        theme_dialog_height,
        pip_min_width,
        subtitle_dialog_width,
        subtitle_dialog_height,
        subtitle_progress_dialog_width,
        subtitle_progress_dialog_height,
    )


def get_metrics(widget: QWidget) -> Metrics:
    handle = widget.window().windowHandle() if widget.window() else None
    screen = handle.screen() if handle else QGuiApplication.primaryScreen()

    if screen is None:
        return _build_metrics(min(BASE_WIDTH, BASE_HEIGHT), 1.0)

    geo = screen.geometry()
    return _build_metrics(
        min(geo.width(), geo.height()),
        screen.devicePixelRatio(),
    )
