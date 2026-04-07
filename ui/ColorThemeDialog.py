from __future__ import annotations

import sys

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QLinearGradient, QPainter, QPalette, QPen
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QSizePolicy, QWidget

from models.ThemeColor import ThemeState
from utils import Metrics, get_metrics, res_path
from ui.PlayerControls import PlayPauseButton, StopButton, VolumeControls, ProgressBar, TimePopupFrame


class ColorThemeDialog(QWidget):
    themeApplied = Signal(object)

    def __init__(self, theme_color: ThemeState, metrics: Metrics, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowFlag(Qt.Window, True)
        self.setWindowTitle("Theme Colors")

        self._updating_ui = False
        self._loaded_theme_color = ThemeState(dict(theme_color.colors))
        self._theme_color = ThemeState(dict(theme_color.colors))
        self._theme_keys = list(self._theme_color.DEFAULTS.keys())
        self._current_color = QColor(255, 255, 255)
        self._metrics = metrics
        self._last_submitted_hex = ""

        self._init_constants()
        self._build_ui()
        self._populate_theme_keys()
        self._select_initial_key()

        self.setMinimumSize(self._metrics.theme_dialog_width, self._metrics.theme_dialog_height)
        self.setMaximumSize(self._metrics.theme_dialog_width * 1.5, self._metrics.theme_dialog_height * 1.5)

    def _init_constants(self):
        self.picker_frame_offset = SaturationValuePicker.CONTENT_MARGIN
        self.icon_size = max(12, self._metrics.icon_size)
        self.gap = max(6, self._metrics.icon_size // 2)
        self.sv_picker_size = max(150, int(self.icon_size * 15))
        self.hue_slider_x = self.sv_picker_size + self.gap
        self.hex_input_height = max(15, int(self.icon_size * 1.3))
        self.hex_input_x = self.hue_slider_x + 2 * self.icon_size + 5
        self.color_swatch_height = max(36, int(self.icon_size * 3))
        self.button_width = max(60, int(self.icon_size * 5))
        self.button_height = max(18, int(self.icon_size * 1.5))
        self.theme_color_list_width = 2 * self.button_width + self.icon_size

    def get_theme_color(self) -> ThemeState:
        return ThemeState(dict(self._theme_color.colors))

    def apply_metrics(self, metrics: Metrics):
        self._metrics = metrics
        self._init_constants()
        self.interface_preview.apply_metrics(metrics)
        self.setMinimumSize(self._metrics.theme_dialog_width, self._metrics.theme_dialog_height)
        self.resize(max(self.width(), self.minimumWidth()), max(self.height(), self.minimumHeight()))
        self.updateGeometry()

    def set_theme_color(self, theme_color: ThemeState):
        self._theme_color = ThemeState(dict(theme_color.colors))
        key = self._current_theme_key()
        if key is not None:
            self._sync_inputs_from_color(self._theme_qcolor(key))

    def _build_ui(self):
        # List of theme color entries available for editing.
        self.color_list_label = QLabel("Select Item:", self)
        self.color_list_widget = QListWidget(self)
        self.color_list_widget.currentItemChanged.connect(self._on_theme_key_changed)

        # Main palette for selecting saturation and value.
        self.color_picker = SaturationValuePicker(self)
        self.color_picker.colorChanged.connect(self._on_saturation_value_changed)

        # Vertical slider for selecting hue.
        self.hue_slider = HueSlider(self)
        self.hue_slider.hueChanged.connect(self._on_hue_changed)

        # Input field for manual HEX color entry.
        self.hex_input = QLineEdit(self)
        self.hex_input.setPlaceholderText("#RRGGBB")
        self.hex_input.returnPressed.connect(self._on_hex_input_changed)
        self.hex_input.editingFinished.connect(self._on_hex_input_changed)

        # Small swatch showing the currently selected color.
        self.color_swatch = QLabel(self)
        self.color_swatch.setFrameShape(QFrame.Box)

        self.interface_preview_frame = QWidget(self)

        # Mini preview of the player interface with the current theme.
        self.interface_preview = InterfacePreview(self._theme_color, self._metrics, self)
        self.interface_preview.raise_()

        # Button to reset only the currently selected color.
        self.reset_color_button = QPushButton("Reset Color", self)
        self.reset_color_button.clicked.connect(self._reset_current_color)

        # Button to reset the entire theme.
        self.reset_theme_button = QPushButton("Reset All", self)
        self.reset_theme_button.clicked.connect(self._reset_all_colors)

        # Button to reset the entire theme to the built-in defaults.
        self.reset_default_button = QPushButton("Reset Default", self)
        self.reset_default_button.clicked.connect(self._reset_all_to_defaults)

        # Button to apply the current theme.
        self.apply_theme_button = QPushButton("Apply", self)
        self.apply_theme_button.clicked.connect(self._apply_and_close)

        # Button to close the dialog window.
        self.close_dialog_button = QPushButton("Close", self)
        self.close_dialog_button.clicked.connect(self.close)

    def resizeEvent(self, event):
        super().resizeEvent(event)

        width = self.width()
        height = self.height()

        sv_picker_y = height - self.sv_picker_size - self.gap
        color_swatch_y = sv_picker_y + self.hex_input_height + self.icon_size
        reset_theme_button_y = height - self.button_height - self.gap
        reset_color_button_y = reset_theme_button_y - self.button_height - self.gap
        reset_default_button_y = reset_color_button_y - self.button_height - self.gap
        close_dialog_button_x = width - self.gap - self.button_width
        apply_theme_button_x = close_dialog_button_x - self.button_width - self.icon_size
        color_list_x = width - self.gap - self.theme_color_list_width
        color_list_y = sv_picker_y + self.hex_input_height
        color_list_height = height - color_list_y - 3 * self.gap - self.button_height

        interface_preview_frame_height = sv_picker_y - 2 * self.gap
        interface_preview_frame_width = width - 2 * self.gap

        self.color_picker.setGeometry(self.gap - self.picker_frame_offset, sv_picker_y, self.sv_picker_size + self.picker_frame_offset, self.sv_picker_size + self.picker_frame_offset)
        self.hue_slider.setGeometry(self.hue_slider_x, sv_picker_y, self.button_height, self.sv_picker_size + self.picker_frame_offset)
        self.hex_input.setGeometry(self.hex_input_x, sv_picker_y + self.picker_frame_offset, self.button_width, self.hex_input_height)
        self.color_swatch.setGeometry(self.hex_input_x, color_swatch_y, self.button_width, self.color_swatch_height)
        self.reset_default_button.setGeometry(self.hex_input_x, reset_default_button_y, self.button_width, self.button_height)
        self.reset_theme_button.setGeometry(self.hex_input_x, reset_theme_button_y, self.button_width, self.button_height)
        self.reset_color_button.setGeometry(self.hex_input_x, reset_color_button_y, self.button_width, self.button_height)
        self.close_dialog_button.setGeometry(close_dialog_button_x, reset_theme_button_y, self.button_width, self.button_height)
        self.apply_theme_button.setGeometry(apply_theme_button_x, reset_theme_button_y, self.button_width, self.button_height)
        self.color_list_label.setGeometry(color_list_x, sv_picker_y + self.picker_frame_offset, self.theme_color_list_width, self.icon_size)
        self.color_list_widget.setGeometry(color_list_x, color_list_y + self.gap, self.theme_color_list_width, color_list_height)
        self.interface_preview_frame.setGeometry(self.gap, self.gap, interface_preview_frame_width, interface_preview_frame_height)
        self.interface_preview.setGeometry(self.interface_preview_frame.geometry().adjusted(1, 1, -1, -1))

    def _populate_theme_keys(self):
        for key in self._theme_keys:
            item = QListWidgetItem(self._theme_color.DISPLAY_NAMES.get(key, key))
            item.setData(Qt.UserRole, key)
            self.color_list_widget.addItem(item)

    def _select_initial_key(self):
        if self.color_list_widget.count() > 0:
            self.color_list_widget.setCurrentRow(0)

    def _current_theme_key(self) -> str | None:
        current_item = self.color_list_widget.currentItem()
        return current_item.data(Qt.UserRole) if current_item is not None else None

    def _theme_qcolor(self, key: str) -> QColor:
        red, green, blue = self._theme_color.get(key)
        return QColor(red, green, blue)

    def _store_current_color(self, color: QColor):
        key = self._current_theme_key()
        if key is None or not color.isValid():
            return
        self._theme_color.set(key, (color.red(), color.green(), color.blue()))

    def _apply_color(self, color: QColor):
        if not color.isValid():
            return
        self._store_current_color(color)
        self._sync_inputs_from_color(color)

    def _sync_inputs_from_color(self, color: QColor):
        hue, saturation, value, _ = color.getHsvF()
        if hue < 0:
            hue = 0.0

        self._current_color = QColor(color)
        current_hue = hue * 359.0
        hex_color = color.name(QColor.HexRgb).upper()

        self._updating_ui = True
        try:
            self.hue_slider.set_hue(current_hue)
            self.color_picker.set_hue(current_hue)
            self.color_picker.set_sv(saturation, value)
            self.hex_input.setText(hex_color)
            self._last_submitted_hex = hex_color
            self.color_swatch.setStyleSheet(
                f"background-color: {color.name(QColor.HexRgb)}; border: 1px solid #666;"
            )
            self.interface_preview_frame.setStyleSheet(
                "background-color: #666;"
            )
            self.interface_preview.update_theme(self._theme_color)
        finally:
            self._updating_ui = False

    def _on_theme_key_changed(self, current: QListWidgetItem | None, previous: QListWidgetItem | None):
        if current is None:
            return
        key = current.data(Qt.UserRole)
        if key is not None:
            self._sync_inputs_from_color(self._theme_qcolor(key))

    def _on_hue_changed(self, hue: float):
        if self._updating_ui:
            return
        _, saturation, value, _ = self._current_color.getHsvF()
        self._apply_color(QColor.fromHsvF(hue / 359.0, saturation, value))

    def _on_saturation_value_changed(self, saturation: float, value: float):
        if self._updating_ui:
            return
        hue, _, _, _ = self._current_color.getHsvF()
        if hue < 0:
            hue = 0.0
        self._apply_color(QColor.fromHsvF(hue, saturation, value))

    def _on_hex_input_changed(self):
        if self._updating_ui:
            return

        raw_value = self.hex_input.text().strip()
        if not raw_value:
            return

        if not raw_value.startswith("#"):
            raw_value = f"#{raw_value}"

        normalized_value = raw_value.upper()
        if normalized_value == self._last_submitted_hex:
            self._updating_ui = True
            try:
                self.hex_input.setText(self._last_submitted_hex)
            finally:
                self._updating_ui = False
            return

        color = QColor(raw_value)
        if not color.isValid():
            key = self._current_theme_key()
            if key is not None:
                self._sync_inputs_from_color(self._theme_qcolor(key))
            return

        self._apply_color(color)

    def _reset_current_color(self):
        key = self._current_theme_key()
        if key is None:
            return
        self._theme_color.set(key, self._loaded_theme_color.get(key))
        self._sync_inputs_from_color(self._theme_qcolor(key))

    def _reset_all_colors(self):
        self._theme_color = ThemeState(dict(self._loaded_theme_color.colors))
        key = self._current_theme_key()
        if key is not None:
            self._sync_inputs_from_color(self._theme_qcolor(key))

    def _reset_all_to_defaults(self):
        self._theme_color = ThemeState()
        key = self._current_theme_key()
        if key is not None:
            self._sync_inputs_from_color(self._theme_qcolor(key))

    def _apply_and_close(self):
        self.themeApplied.emit(self.get_theme_color())
        self.close()

class SaturationValuePicker(QWidget):
    colorChanged = Signal(float, float)
    CONTENT_MARGIN = 8

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._hue = 0.0
        self._saturation = 0.0
        self._value = 1.0

    def set_hue(self, hue: float):
        self._hue = max(0.0, min(359.0, hue))
        self.update()

    def set_sv(self, saturation: float, value: float):
        self._saturation = max(0.0, min(1.0, saturation))
        self._value = max(0.0, min(1.0, value))
        self.update()

    def _content_rect(self) -> QRect:
        margin = self.CONTENT_MARGIN
        return self.rect().adjusted(margin, margin, -margin, -margin)

    def _position_to_sv(self, position: QPoint) -> tuple[float, float]:
        rect = self._content_rect()
        if rect.width() <= 1 or rect.height() <= 1:
            return self._saturation, self._value

        x = max(rect.left(), min(rect.right(), position.x()))
        y = max(rect.top(), min(rect.bottom(), position.y()))
        saturation = (x - rect.left()) / max(1, rect.width() - 1)
        value = 1.0 - ((y - rect.top()) / max(1, rect.height() - 1))
        return saturation, value

    def _emit_position(self, position: QPoint):
        saturation, value = self._position_to_sv(position)
        self.colorChanged.emit(saturation, value)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._emit_position(event.position().toPoint())

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            self._emit_position(event.position().toPoint())

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self._content_rect()
        base_color = QColor.fromHsv(int(self._hue), 255, 255)

        saturation_gradient = QLinearGradient(rect.topLeft(), rect.topRight())
        saturation_gradient.setColorAt(0.0, QColor(255, 255, 255))
        saturation_gradient.setColorAt(1.0, base_color)
        painter.fillRect(rect, saturation_gradient)

        value_gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        value_gradient.setColorAt(0.0, QColor(0, 0, 0, 0))
        value_gradient.setColorAt(1.0, QColor(0, 0, 0, 255))
        painter.fillRect(rect, value_gradient)

        painter.setPen(QPen(QColor(255, 255, 255), 2))
        handle_x = rect.left() + int(self._saturation * (rect.width() - 1))
        handle_y = rect.top() + int((1.0 - self._value) * (rect.height() - 1))
        painter.drawEllipse(QPoint(handle_x, handle_y), 6, 6)
        painter.setPen(QPen(QColor(0, 0, 0), 1))
        painter.drawEllipse(QPoint(handle_x, handle_y), 7, 7)


class HueSlider(QWidget):
    hueChanged = Signal(float)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._hue = 0.0

    def set_hue(self, hue: float):
        self._hue = max(0.0, min(359.0, hue))
        self.update()

    def _content_rect(self) -> QRect:
        return self.rect().adjusted(10, 8, -10, -8)

    def _position_to_hue(self, position: QPoint) -> float:
        rect = self._content_rect()
        if rect.height() <= 1:
            return self._hue

        y = max(rect.top(), min(rect.bottom(), position.y()))
        hue = 359.0 * (1.0 - ((y - rect.top()) / max(1, rect.height() - 1)))
        return max(0.0, min(359.0, hue))

    def _emit_position(self, position: QPoint):
        self.hueChanged.emit(self._position_to_hue(position))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._emit_position(event.position().toPoint())

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            self._emit_position(event.position().toPoint())

    def paintEvent(self, event):
        painter = QPainter(self)
        rect = self._content_rect()

        hue_gradient = QLinearGradient(rect.bottomLeft(), rect.topLeft())
        hue_gradient.setColorAt(0.0, QColor.fromHsv(0, 255, 255))
        hue_gradient.setColorAt(1.0 / 6.0, QColor.fromHsv(60, 255, 255))
        hue_gradient.setColorAt(2.0 / 6.0, QColor.fromHsv(120, 255, 255))
        hue_gradient.setColorAt(3.0 / 6.0, QColor.fromHsv(180, 255, 255))
        hue_gradient.setColorAt(4.0 / 6.0, QColor.fromHsv(240, 255, 255))
        hue_gradient.setColorAt(5.0 / 6.0, QColor.fromHsv(300, 255, 255))
        hue_gradient.setColorAt(1.0, QColor.fromHsv(359, 255, 255))
        painter.fillRect(rect, hue_gradient)

        painter.setPen(QPen(QColor(255, 255, 255), 2))
        handle_y = rect.top() + int((1.0 - (self._hue / 359.0)) * (rect.height() - 1))
        painter.drawLine(rect.left() - 4, handle_y, rect.right() + 4, handle_y)
        painter.setPen(QPen(QColor(0, 0, 0), 1))
        painter.drawLine(rect.left() - 4, handle_y + 2, rect.right() + 4, handle_y + 2)


class InterfacePreview(QWidget):
    def __init__(self, theme_color: ThemeState, metrics: Metrics, parent: QWidget | None = None):
        super().__init__(parent)
        self._theme_color = theme_color
        self._metrics = metrics
        self._init_constants()

        self.setMinimumHeight(max(220, 8 * self.icon_size))
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self._build_ui()
        self._configure_preview()
        self.update_theme(self._theme_color)

    def _init_constants(self):
        self.icon_size = max(12, int(self._metrics.icon_size * 0.7))
        self.font_size = max(10, int(self._metrics.font_size * 0.85))
        self.gap = max(6, self._metrics.icon_size // 2)
        self.half_gap = self.gap // 2
        self.double_gap = 2 * self.gap
        self.quad_gap = 4 * self.gap
        self.player_frame_gap = 2 * self.icon_size
        self.player_frame_min_height = 3 * self.icon_size
        self.volume_controls_width = 6 * self.icon_size
        self.time_popup_height = int(1.5 * self.icon_size)
        self.label_width = 2 * self.icon_size
        self.menu_label_width = 3 * self.icon_size
        self.popup_width = 4 * self.icon_size
        self.progress_bar_x = self.double_gap + self.label_width
        self.stop_button_x = self.double_gap + self.icon_size
        self.popup_offset_y = self.time_popup_height + self.gap

    def _build_ui(self):
        self.media_label = QLabel("Media", self, alignment=Qt.AlignCenter)
        self.audio_label = QLabel("Audio", self, alignment=Qt.AlignCenter)
        self.player_frame = QWidget(self)
        self.play_button = PlayPauseButton(self, theme_color=self._theme_color, scale_factor=self._metrics.scale_factor)
        self.stop_button = StopButton(self, theme_color=self._theme_color, scale_factor=self._metrics.scale_factor)
        self.volume_controls = VolumeControls(self, theme_color=self._theme_color, scale_factor=self._metrics.scale_factor)
        self.current_time = QLabel("15:33", self, alignment=Qt.AlignCenter)
        self.progress_bar = ProgressBar(self, theme_color=self._theme_color)
        self.total_time = QLabel("45:03", self, alignment=Qt.AlignCenter)
        self.time_popup = TimePopupFrame(self, theme_color=self._theme_color, scale_factor=self._metrics.scale_factor)
        self.time_popup_label = QLabel("22:05", self, alignment=Qt.AlignCenter)
        self.player_frame.setAttribute(Qt.WA_StyledBackground, True)

        self._text_labels = (
            self.media_label,
            self.audio_label,
            self.current_time,
            self.total_time,
            self.time_popup_label,
        )
        self._control_buttons = (
            self.play_button,
            self.stop_button,
            self.volume_controls.volume_button,
        )

    def _configure_preview(self):
        for item in self.findChildren(QWidget):
            item.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        self.play_button.set_playing(True)
        self.volume_controls.volume_bar.set_volume(0.5)
        self.progress_bar.set_value(0.35)
        self.apply_metrics(self._metrics)

    def apply_metrics(self, metrics: Metrics):
        self._metrics = metrics
        self._init_constants()
        self.setMinimumHeight(max(220, 8 * self.icon_size))

        for label in self._text_labels:
            font = label.font()
            font.setPixelSize(self.font_size)
            label.setFont(font)

        scale_factor = self._metrics.scale_factor
        for button in self._control_buttons:
            button.scale_factor = scale_factor

        self.volume_controls.apply_metrics(scale_factor)
        self.time_popup.scale_factor = scale_factor

        self.updateGeometry()
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)

        width = self.width()
        height = self.height()

        progress_bar_width = width - self.quad_gap - 2 * self.label_width
        right_time_x = width - self.gap - self.label_width
        volume_controls_x = width - self.gap - self.volume_controls_width
        popup_x = width // 2 - self.icon_size

        player_frame_height = max(self.player_frame_min_height, height - 3 * self.player_frame_gap)
        progress_bar_y = player_frame_height + self.player_frame_gap + self.gap
        play_button_y = progress_bar_y + self.gap + self.icon_size
        time_popup_y = progress_bar_y - self.popup_offset_y

        self.player_frame.setGeometry(0, self.player_frame_gap, width, player_frame_height)
        self.media_label.setGeometry(self.gap, self.half_gap, self.menu_label_width, self.icon_size)
        self.audio_label.setGeometry(self.gap + self.menu_label_width, self.half_gap, self.menu_label_width, self.icon_size)
        self.current_time.setGeometry(self.gap, progress_bar_y, self.label_width, self.icon_size)
        self.progress_bar.setGeometry(self.progress_bar_x, progress_bar_y, progress_bar_width, self.icon_size)
        self.total_time.setGeometry(right_time_x, progress_bar_y, self.label_width, self.icon_size)
        self.play_button.setGeometry(self.gap, play_button_y, self.icon_size, self.icon_size)
        self.stop_button.setGeometry(self.stop_button_x, play_button_y, self.icon_size, self.icon_size)
        self.volume_controls.setGeometry(volume_controls_x, play_button_y, self.volume_controls_width, self.icon_size)
        self.time_popup.setGeometry(popup_x, time_popup_y, self.popup_width, self.time_popup_height)
        self.time_popup_label.setGeometry(popup_x, time_popup_y, self.popup_width, self.icon_size)

    def update_theme(self, theme_color: ThemeState):
        self._theme_color = ThemeState(dict(theme_color.colors))
        self._apply_palette()
        self._apply_controls()
        self.update()

    def _apply_palette(self):
        panel_color = QColor(*self._theme_color.get("panel_bg_color"))
        self._set_background(self, panel_color)
        self._set_background(self.player_frame, QColor(0, 0, 0))

        text_color = QColor(*self._theme_color.get("text_color"))
        popup_text_color = QColor(*self._theme_color.get("time_popup_text_color"))

        for label in self._text_labels[:-1]:
            palette = label.palette()
            palette.setColor(QPalette.WindowText, text_color)
            label.setPalette(palette)

        popup_palette = self.time_popup_label.palette()
        popup_palette.setColor(QPalette.WindowText, popup_text_color)
        self.time_popup_label.setPalette(popup_palette)

    def _apply_controls(self):
        normal = self._theme_color.get("control_button_color")
        hovered = self._theme_color.get("control_button_color_hovered")
        pressed = self._theme_color.get("control_button_color_pressed")

        for button in self._control_buttons:
            button.bg_color = normal
            button.bg_color_hovered = hovered
            button.bg_color_pressed = pressed
            button.update()

        self.time_popup.bg_color = self._theme_color.get("time_popup_color")
        self.time_popup.bg_color_hovered = self.time_popup.bg_color
        self.time_popup.bg_color_pressed = self.time_popup.bg_color
        self.time_popup.update()

        self._apply_bar_colors()

    def _apply_bar_colors(self):
        self.volume_controls.volume_bar.active_bg_color = self._theme_color.get("volume_bar_color_active")
        self.volume_controls.volume_bar.inactive_bg_color = self._theme_color.get("volume_bar_color_inactive")
        self.volume_controls.volume_bar.update()

        self.progress_bar.active_bg_color = self._theme_color.get("progress_bar_color_active")
        self.progress_bar.inactive_bg_color = self._theme_color.get("control_button_color")
        self.progress_bar.update()

    def _set_background(self, widget: QWidget, color: QColor):
        widget.setAutoFillBackground(True)
        palette = widget.palette()
        palette.setColor(QPalette.Window, color)
        widget.setPalette(palette)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(res_path("assets/logo.ico")))

    preview_host = QWidget()
    dialog = ColorThemeDialog(ThemeState(), get_metrics(preview_host))
    dialog.show()

    sys.exit(app.exec())
