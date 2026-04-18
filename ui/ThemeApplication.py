from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QWidget

from models.ThemeColor import ThemeState


RGB = tuple[int, int, int]


@dataclass(frozen=True)
class ButtonTheme:
    normal: RGB
    hovered: RGB
    pressed: RGB


@dataclass(frozen=True)
class BarTheme:
    active: RGB
    inactive: RGB


def theme_rgb(theme, key: str, fallback: RGB | None = None) -> RGB:
    value = _read_theme_value(theme, key, fallback)
    return _normalize_rgb(value, _fallback_rgb(key, fallback))


def theme_qcolor(theme, key: str, fallback: RGB | None = None) -> QColor:
    return QColor(*theme_rgb(theme, key, fallback))


def button_theme(theme) -> ButtonTheme:
    return ButtonTheme(
        normal=theme_rgb(theme, "control_button_color"),
        hovered=theme_rgb(theme, "control_button_color_hovered"),
        pressed=theme_rgb(theme, "control_button_color_pressed"),
    )


def popup_button_theme(theme) -> ButtonTheme:
    color = theme_rgb(theme, "time_popup_color")
    return ButtonTheme(normal=color, hovered=color, pressed=color)


def bar_theme(theme, active_key: str, inactive_key: str) -> BarTheme:
    return BarTheme(
        active=theme_rgb(theme, active_key),
        inactive=theme_rgb(theme, inactive_key),
    )


def apply_button_theme(button, colors: ButtonTheme):
    button.bg_color = colors.normal
    button.bg_color_hovered = colors.hovered
    button.bg_color_pressed = colors.pressed
    button.update()


def apply_bar_theme(bar, colors: BarTheme):
    bar.active_bg_color = colors.active
    bar.inactive_bg_color = colors.inactive
    bar.update()


def apply_window_palette(widget: QWidget, color: QColor):
    widget.setAutoFillBackground(True)
    palette = widget.palette()
    palette.setColor(QPalette.Window, color)
    widget.setPalette(palette)


def set_label_text_color(label, color: QColor):
    palette = label.palette()
    palette.setColor(QPalette.WindowText, color)
    label.setPalette(palette)


def rgb_css(color: RGB) -> str:
    return f"rgb({color[0]}, {color[1]}, {color[2]})"


def _read_theme_value(theme, key: str, fallback: RGB | None):
    getter = getattr(theme, "get", None)
    if not callable(getter):
        return fallback
    try:
        return getter(key, fallback)
    except TypeError:
        value = getter(key)
        return fallback if value is None else value


def _fallback_rgb(key: str, fallback: RGB | None) -> RGB:
    if fallback is not None:
        return _normalize_rgb(fallback, (0, 0, 0))
    default_value = ThemeState().get(key)
    return _normalize_rgb(default_value, (0, 0, 0))


def _normalize_rgb(value, fallback: RGB) -> RGB:
    if isinstance(value, (tuple, list)) and len(value) >= 3:
        try:
            return int(value[0]), int(value[1]), int(value[2])
        except (TypeError, ValueError):
            return fallback
    return fallback
