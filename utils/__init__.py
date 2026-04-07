from dataclasses import dataclass

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget

import os
import sys

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

def res_path(relative_path: str) -> str:
    base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base_path, relative_path)

def get_metrics(widget: QWidget) -> Metrics:
    handle = widget.window().windowHandle() if widget.window() else None
    screen = handle.screen() if handle else QGuiApplication.primaryScreen() 

    if screen is None:
        min_window_side = min(BASE_WIDTH, BASE_HEIGHT)
        scale_factor = 1.0
        window_width = int(min_window_side * 0.8)
        window_height = int(min_window_side * 0.5)
        icon_size = int(min_window_side / 70 * scale_factor)
        font_size = int(icon_size * 0.7)
        menu_width = int(min_window_side * 0.1)
        theme_dialog_height = int(min_window_side / 1.75) 
        theme_dialog_width = theme_dialog_height // 2
        pip_min_width = int(min_window_side / 5.2)

        return Metrics(min_window_side, scale_factor, window_width, window_height, icon_size, font_size, menu_width, theme_dialog_width, theme_dialog_height, pip_min_width)
    
    geo = screen.geometry()

    min_window_side = min(geo.width(), geo.height())
    scale_factor = screen.devicePixelRatio()
    window_width = int(min_window_side * 0.8)
    window_height = int(min_window_side * 0.5)
    icon_size = int(min_window_side / 70 * scale_factor)
    font_size = int(icon_size * 0.7)
    menu_width = int(min_window_side * 0.1)
    theme_dialog_height = int(min_window_side / 1.75)
    theme_dialog_width = min_window_side // 2
    pip_min_width = int(min_window_side / 5.2)

    return Metrics(min_window_side, scale_factor, window_width, window_height, icon_size, font_size, menu_width, theme_dialog_width, theme_dialog_height, pip_min_width)

def _color_from_state(state: str = "normal", bg_color: tuple[int, int, int] = (37, 37, 37)) -> tuple[int, int, int]:
        r, g, b = bg_color
        factor = (-1.0) if r < 145 or g < 145 or b < 145 else (1.0)
        if state == "pressed":
            return (
                max(0, min(255, int(r - (r * 0.3) * factor))),
                max(0, min(255, int(g - (g * 0.3) * factor))),
                max(0, min(255, int(b - (b * 0.3) * factor))),
            )
        if state == "hovered":
            return (
                max(0, min(255, int(r - (r * 0.15) * factor))),
                max(0, min(255, int(g - (g * 0.15) * factor))),
                max(0, min(255, int(b - (b * 0.15) * factor))),
            )
        if state == "inactive":
            return (
                max(0, min(255, int(0.3 * r))),
                max(0, min(255, int(0.3 * g))),
                max(0, min(255, int(0.3 * b))),
            )
        if state == "separator":
            return (
                max(0, min(255, int(r - (r * 0.6) * factor))),
                max(0, min(255, int(g - (g * 0.6) * factor))),
                max(0, min(255, int(b - (b * 0.6) * factor))),
            )
        return bg_color

def _format_ms(ms: int) -> str:
    total_seconds = max(0, ms // 1000)
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"

def _normalize_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))

def build_window_title(
    media_path: str | None = None,
    base_title: str = "A1lPlayer",
    max_media_title_length: int = 36,
) -> str:
    if not media_path:
        return base_title

    media_name = os.path.splitext(os.path.basename(media_path))[0].strip()
    if not media_name:
        return base_title

    if len(media_name) > max_media_title_length:
        media_name = f"{media_name[:max_media_title_length].rstrip()}..."

    return f"{base_title}: {media_name}"
