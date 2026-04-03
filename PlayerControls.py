from __future__ import annotations

from typing import Dict, List, Tuple

import os

from PySide6.QtWidgets import QWidget, QAbstractButton, QLabel
from PySide6.QtGui import QPainter, QColor, QMouseEvent, QWheelEvent, QImage, QPixmap, QPalette
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtCore import Qt, QRectF, QTimer, Signal, QEvent

from utils import Metrics, res_path, _format_ms
from ThemeColor import ThemeColor

class PlayerControls(QWidget):
    def __init__(self, parent: QWidget, metrics: Metrics, theme_color: ThemeColor | None = None):
        super().__init__(parent)
        self.metrics = metrics
        self.theme_color = theme_color

        self.current_time = QLabel("00:00", self)
        self.current_time.setAlignment(Qt.AlignCenter)
        self.progress_bar = ProgressBar(self, theme_color = self.theme_color)
        self.total_time = QLabel("00:00", self)
        self.total_time.setAlignment(Qt.AlignCenter)

        self._setup_font()

        sf = self.metrics.scale_factor
        self.play_pause_button = PlayPauseButton(self, theme_color=self.theme_color, scale_factor=sf)
        self.rewind_lbutton = RewindButton(self, direction="left", theme_color=self.theme_color, scale_factor=sf)
        self.stop_button = StopButton(self, theme_color=self.theme_color, scale_factor=sf)
        self.rewind_rbutton = RewindButton(self, direction="right", theme_color=self.theme_color, scale_factor=sf)
        self.fullscreen_button = FullscreenButton(self, theme_color=self.theme_color, scale_factor=sf)
        self.pip_button = PiPButton(self, theme_color=self.theme_color, scale_factor=sf)

        self.volume_controls = VolumeControls(self, theme_color=self.theme_color, scale_factor=sf)

        self.buttons: List[BaseButton] = [
            self.play_pause_button,
            self.rewind_lbutton,
            self.stop_button,
            self.rewind_rbutton,
            self.fullscreen_button,
            self.pip_button
        ]

        self.setup_style()

    def apply_metrics(self, metrics: Metrics):
        self.metrics = metrics
        self._setup_font()

        scale_factor = self.metrics.scale_factor
        for button in self.buttons:
            button.scale_factor = scale_factor
        self.volume_controls.apply_metrics(scale_factor)

        self.updateGeometry()
        self.update()

    def resizeEvent(self, event: QEvent):
        super().resizeEvent(event)

        width = self.width()
        height = self.height()

        icon_size = self.metrics.icon_size
        gap = max(1, int(icon_size * 0.7))

        first_line_y = int(height / 4.0)
        second_line_y = int(3 * height / 4.0)

        label_width = icon_size * 3
        label_y = int(first_line_y - icon_size / 2.0)
        progress_bar_width = max(width - gap * 2 - label_width * 2, label_width)
        total_time_x = width - gap - label_width

        buttons_y = int(second_line_y - (1.333 * icon_size) / 2.0)

        volume_controls_width = int(icon_size * 6)
        volume_controls_x = width - volume_controls_width - int(1.333 * icon_size)

        self.current_time.setGeometry(gap, label_y, label_width, icon_size)
        self.progress_bar.setGeometry(gap + label_width, label_y, progress_bar_width, icon_size)
        self.total_time.setGeometry(total_time_x, label_y, label_width, icon_size)

        for i, button in enumerate(self.buttons):
            extra_gap = (1 if i > 0 else 0) * icon_size + (1 if i > 3 else 0) * icon_size
            button_x = 2 * gap + extra_gap + (i * (gap + icon_size))
            button.setGeometry(button_x, buttons_y, icon_size, icon_size)

        self.volume_controls.setGeometry(volume_controls_x, buttons_y, volume_controls_width, icon_size)

    def setup_style(self):
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAutoFillBackground(True)
        palette = self.palette()
        color = self.theme_color.get("panel_bg_color")
        palette.setColor(QPalette.Window, QColor(*color))
        self.setPalette(palette)

    def _setup_font(self):
        font = self.current_time.font()
        color = self.theme_color.get("text_color")
        font.setPixelSize(self.metrics.font_size)
        palette = self.current_time.palette()
        palette.setColor(QPalette.WindowText, QColor(*color))
        self.current_time.setPalette(palette)
        self.total_time.setPalette(palette)

        self.current_time.setFont(font)
        self.total_time.setFont(font)

    def toggle_play_pause(self, playing: bool):
        self.play_pause_button.set_playing(playing)

    def toggle_fullscreen(self, fullscreen: bool):
        self.fullscreen_button.set_fullscreen(fullscreen)

    def toggle_progress_seekable(self, seekable: bool):
        self.progress_bar.set_seekable(seekable)

    def toggle_muted(self, muted: bool):
        self.volume_controls.volume_button.set_muted(muted)

    def current_volume_percent(self) -> int:
        return int(round(self.volume_controls.volume_bar.volume * 100))

    def update_timing(self, current_ms: int, total_ms: int):
        current_ms = current_ms if current_ms and current_ms > 0 else 0
        total_ms = total_ms if total_ms and total_ms > 0 else 0

        self.current_time.setText(_format_ms(current_ms))
        self.total_time.setText(_format_ms(total_ms))

        if total_ms > 0:
            self.progress_bar.set_value(current_ms / total_ms)
        else:
            self.progress_bar.set_value(0.0)

class TimePopup(QWidget):
    def __init__(self, parent: QWidget | None, metrics: Metrics, theme_color: ThemeColor | None = None):
        super().__init__(parent)
        self.setWindowFlags(Qt.ToolTip | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)

        self.metrics = metrics
        self.frame = TimePopupFrame(self, theme_color, scale_factor=self.metrics.scale_factor)

        self.time_label = QLabel("00:00", self)
        self.time_label.setAlignment(Qt.AlignCenter)
        font_color = theme_color.get("time_popup_text_color")
        palette = self.time_label.palette()
        palette.setColor(QPalette.WindowText, QColor(*font_color))
        self.time_label.setPalette(palette)

        font = self.time_label.font()
        font.setPixelSize(self.metrics.font_size)
        self.time_label.setFont(font)

    def resizeEvent(self, event):
        super().resizeEvent(event)

        width = self.width()
        height = self.height()

        label_height = int(height * 0.8) - 2

        self.frame.setGeometry(0, 0, width, height)
        self.time_label.setGeometry(0, 0, width, label_height)

    def preferred_size(self) -> tuple[int, int]:
        w = max(1, int(self.metrics.icon_size * 3))
        h = max(1, int(self.metrics.icon_size * 1.5))
        return w, h

    def set_time(self, ms: int):
        self.time_label.setText(_format_ms(ms))

    def apply_metrics(self, metrics: Metrics):
        self.metrics = metrics

        font = self.time_label.font()
        font.setPixelSize(self.metrics.font_size)
        self.time_label.setFont(font)

        self.frame.scale_factor = self.metrics.scale_factor

        self.updateGeometry()
        self.update()

class BaseButton(QAbstractButton):
    _pixmap_cache: Dict[Tuple[str, int, int, int, int], QPixmap] = {}

    def __init__(self, parent: QWidget | None = None, theme_color: ThemeColor | None = None, scale_factor: float = 1.0, var: str | None = None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.bg_color = theme_color.get("time_popup_color") if var == "time_popup" else theme_color.get("control_button_color")
        self.bg_color_hovered = theme_color.get("control_button_color_hovered")
        self.bg_color_pressed = theme_color.get("control_button_color_pressed")
        self.scale_factor = scale_factor
        self.is_hovered = False

    def _get_svg_filename(self) -> str:
        raise NotImplementedError("Subclasses must implement _get_svg_filename")

    def _get_icon_rect(self) -> QRectF:
        width = self.width()
        height = self.height()
        return QRectF(0, 0, max(1, width), max(1, height))

    def _get_bg_color(self) -> List[int]:
        r, g, b = self.bg_color
        if self.isDown():
            return self.bg_color_pressed
        if self.is_hovered:
            return self.bg_color_hovered
        return self.bg_color 

    def _render_tinted_svg(self, svg_filename: str, width: int, height: int, color_rgb: List[int], dpr: float = 1.0) -> QPixmap:
        cache_key = (svg_filename, width, height, color_rgb[0], color_rgb[1], color_rgb[2], dpr)
        cached = self._pixmap_cache.get(cache_key)
        if cached is not None:
            return cached

        svg_path = res_path(os.path.join("assets", svg_filename))
        renderer = QSvgRenderer(svg_path)

        phys_width = max(1, int(width * dpr))
        phys_height = max(1, int(height * dpr))
        image = QImage(phys_width, phys_height, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)

        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        renderer.render(painter, QRectF(0, 0, phys_width, phys_height))

        painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
        painter.fillRect(image.rect(), QColor(*color_rgb))
        painter.end()

        pixmap = QPixmap.fromImage(image)
        pixmap.setDevicePixelRatio(dpr)
        self._pixmap_cache[cache_key] = pixmap
        return pixmap

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        icon_rect = self._get_icon_rect()
        icon_width = int(icon_rect.width())
        icon_height = int(icon_rect.height())
        pixmap = self._render_tinted_svg(self._get_svg_filename(), icon_width, icon_height, self._get_bg_color(), self.scale_factor)
        painter.drawPixmap(int(icon_rect.x()), int(icon_rect.y()), pixmap)

    def enterEvent(self, event: QEvent):
        self.is_hovered = True
        self.update()

    def leaveEvent(self, event: QEvent):
        self.is_hovered = False
        self.update()

    def mouseReleaseEvent(self, event: QEvent):
        return super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event: QEvent):
        hovered_now = self._get_icon_rect().contains(event.position())
        if hovered_now != self.is_hovered:
            self.is_hovered = hovered_now
            self.update()
        return super().mouseMoveEvent(event)


class PlayPauseButton(BaseButton):
    def __init__(self, parent: QWidget | None = None, theme_color: ThemeColor | None = None, scale_factor: float = 1.0):
        super().__init__(parent, theme_color, scale_factor)
        self.is_playing = False

    def set_playing(self, playing: bool):
        self.is_playing = playing
        self.update()

    def _get_svg_filename(self) -> str:
        return "pause.svg" if self.is_playing else "play.svg"


class RewindButton(BaseButton):
    seek_hold = Signal(str)  # направление: "left" или "right"

    LONG_PRESS_DELAY = 500   # мс до активации перемотки
    SEEK_INTERVAL    = 300   # мс между шагами перемотки при удержании

    def __init__(self, parent: QWidget | None = None, theme_color: ThemeColor | None = None, direction: str = "left", scale_factor: float = 1.0):
        super().__init__(parent, theme_color, scale_factor)
        self.direction = direction
        self._is_long_press = False

        self._long_press_timer = QTimer(self)
        self._long_press_timer.setSingleShot(True)
        self._long_press_timer.setInterval(self.LONG_PRESS_DELAY)
        self._long_press_timer.timeout.connect(self._on_long_press_activated)

        self._seek_repeat_timer = QTimer(self)
        self._seek_repeat_timer.setInterval(self.SEEK_INTERVAL)
        self._seek_repeat_timer.timeout.connect(self._emit_seek)

    def _on_long_press_activated(self):
        self._is_long_press = True
        self._emit_seek()
        self._seek_repeat_timer.start()

    def _emit_seek(self):
        self.seek_hold.emit(self.direction)

    def mousePressEvent(self, event: QEvent):
        if event.button() == Qt.LeftButton:
            self._is_long_press = False
            self.setDown(True)
            self.update()
            self._long_press_timer.start()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QEvent):
        if event.button() == Qt.LeftButton:
            self._long_press_timer.stop()
            self._seek_repeat_timer.stop()
            was_long = self._is_long_press
            self._is_long_press = False
            self.setDown(False)
            self.update()
            if not was_long and self.rect().contains(event.position().toPoint()):
                self.clicked.emit()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def _get_svg_filename(self) -> str:
        return "rewindright.svg" if self.direction == "right" else "rewindleft.svg"


class StopButton(BaseButton):
    def __init__(self, parent: QWidget | None = None, theme_color: ThemeColor | None = None, scale_factor: float = 1.0):
        super().__init__(parent, theme_color, scale_factor)

    def _get_svg_filename(self) -> str:
        return "stop.svg"


class FullscreenButton(BaseButton):
    def __init__(self, parent: QWidget | None = None, theme_color: ThemeColor | None = None, scale_factor: float = 1.0):
        super().__init__(parent, theme_color, scale_factor)
        self.is_fullscreen = False

    def set_fullscreen(self, fullscreen: bool):
        self.is_fullscreen = fullscreen
        self.update()

    def _get_svg_filename(self) -> str:
        return "restore.svg" if self.is_fullscreen else "fullscreen.svg"
    
class PiPButton(BaseButton):
    def __init__(self, parent: QWidget | None = None, theme_color: ThemeColor | None = None, scale_factor: float = 1.0):
        super().__init__(parent, theme_color, scale_factor)

    def _get_svg_filename(self) -> str:
        return "pipopen.svg"


class VolumeControls(QWidget):
    def __init__(self, parent: QWidget | None = None, theme_color: ThemeColor | None = None, scale_factor: float = 1.0):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)

        self.volume_button = VolumeButton(self, theme_color=theme_color, scale_factor=scale_factor)
        self.volume_bar = VolumeBar(self, theme_color=theme_color)

    def apply_metrics(self, scale_factor: float):
        self.volume_button.scale_factor = scale_factor
        self.updateGeometry()
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)

        width = self.width()
        height = self.height()

        button_size = int(min(width, height))
        button_y = int((height - button_size) / 2.0)

        bar_x = int(button_size * 1.5)
        bar_height = int(button_size * 0.8)
        bar_y = int((height - bar_height) / 2.0)
        bar_width = max(1, int(round(width - bar_x)))

        self.volume_button.setGeometry(0, button_y, button_size, button_size)
        self.volume_bar.setGeometry(bar_x, bar_y, bar_width, bar_height)


class VolumeBar(QWidget):
    volume_changed = Signal(float)

    def __init__(self, parent: QWidget | None = None, theme_color: ThemeColor | None = None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMouseTracking(True)

        self.active_bg_color = theme_color.get("volume_bar_color_active")
        self.inactive_bg_color = theme_color.get("volume_bar_color_inactive")

        self.volume = 1.0

    def set_volume(self, volume: float):
        self.volume = max(0, min(1.0, volume))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)

        width = self.width()
        height = self.height()

        interval_width = int(width / 10)
        seg_width = int(interval_width * 0.8)

        active_seg = int(self.volume * 10)

        for i in range(10):
            x = int(i * interval_width)
            color = self.active_bg_color if i < active_seg else self.inactive_bg_color

            painter.setBrush(QColor(*color))
            painter.drawRect(x, 0, seg_width, height)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self._update_volume_from_pos(event.position().x())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if event.buttons() & Qt.LeftButton:
            self._update_volume_from_pos(event.position().x())

    def wheelEvent(self, event: QWheelEvent):
        delta = event.angleDelta().y()

        if delta > 0:
            new_volume = min(1.0, self.volume + 0.1)
        else:
            new_volume = max(0.0, self.volume - 0.1)

        self.set_volume(new_volume)
        self.volume_changed.emit(self.volume)

        event.accept()

    def _update_volume_from_pos(self, pos):
        width = self.width()
        interval_width = int(width / 10)

        if pos < 0:
            self.set_volume(0)
        elif pos > width:
            self.set_volume(1.0)
        else:
            seg = int(pos / interval_width) + 1
            self.set_volume(seg / 10.0)

        self.volume_changed.emit(self.volume)


class VolumeButton(BaseButton):
    def __init__(self, parent: QWidget | None = None, theme_color: ThemeColor | None = None, scale_factor: float = 1.0):
        super().__init__(parent, theme_color, scale_factor)
        self.is_muted = False

    def set_muted(self, muted: bool):
        self.is_muted = muted
        self.update()

    def _get_svg_filename(self) -> str:
        return "volumemute.svg" if self.is_muted else "volumenomute.svg"


class ProgressBar(QWidget):
    seek_started = Signal()
    value_changed = Signal(float)
    seek_finished = Signal()
    hover_changed = Signal(float)   # ratio 0..1, при наведении без нажатия
    hover_left = Signal()           # мышь покинула прогресс-бар

    def __init__(self, parent: QWidget | None = None, theme_color: ThemeColor | None = None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMouseTracking(True)

        self.active_bg_color = theme_color.get("progress_bar_color_active")
        self.inactive_bg_color = theme_color.get("progress_bar_color_inactive")

        self.value = 0.0
        self._dragging = False
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def set_seekable(self, seekable: bool):
        self.setAttribute(Qt.WA_TransparentForMouseEvents, not seekable)
        if not seekable and self._dragging:
            self._dragging = False

    def set_value(self, value: float):
        self.value = max(0.0, min(1.0, value))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.setPen(Qt.NoPen)

        width = self.width()
        height = self.height()

        bar_height = int(round(height * 0.6))
        bar_y = int(round((height - bar_height) / 2.0))

        painter.setBrush(QColor(*self.inactive_bg_color))
        painter.drawRect(0, bar_y, width, bar_height)

        painter.setBrush(QColor(*self.active_bg_color))
        painter.drawRect(0, bar_y, int(round(width * self.value)), bar_height)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton and not self._dragging:
            self._dragging = True
            self.seek_started.emit()
            self._update_value_from_pos(event.position().x())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton and self._dragging:
            self._dragging = False
            self.seek_finished.emit()
        return super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        ratio = max(0.0, min(1.0, event.position().x() / max(1, self.width())))
        self.hover_changed.emit(ratio)
        if event.buttons() & Qt.LeftButton:
            self._update_value_from_pos(event.position().x())

    def leaveEvent(self, event):
        self.hover_left.emit()
        super().leaveEvent(event)

    def _update_value_from_pos(self, pos):
        width = self.width()

        if pos < 0:
            self.set_value(0)
        elif pos > width:
            self.set_value(1.0)
        else:
            self.set_value(pos / width)

        self.value_changed.emit(self.value)

class TimePopupFrame(BaseButton):
    def __init__(self, parent: QWidget | None = None, theme_color: ThemeColor | None = None, scale_factor: float = 1.0):
        super().__init__(parent, theme_color, scale_factor, var="time_popup")
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def _get_svg_filename(self) -> str:
        return "timepopup.svg"
