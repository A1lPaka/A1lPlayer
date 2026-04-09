from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    project_root = Path(__file__).resolve().parent.parent
    project_root_str = str(project_root)
    if project_root_str not in sys.path:
        sys.path.insert(0, project_root_str)

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)

from models.ThemeColor import ThemeState
from utils import Metrics, get_metrics, res_path


@dataclass
class SubtitleGenerationDialogResult:
    audio_track_id: int | None
    audio_language: str | None
    device: str | None
    model_size: str
    output_format: str
    output_path: str
    auto_open_after_generation: bool


class ArrowComboBox(QComboBox):
    arrowChanged = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._arrow_down_path = res_path("assets/arrowdown.svg").replace("\\", "/")
        self._arrow_up_path = res_path("assets/arrowup.svg").replace("\\", "/")
        self._arrow_path = self._arrow_down_path

    @property
    def arrow_path(self) -> str:
        return self._arrow_path

    def showPopup(self):
        self._arrow_path = self._arrow_up_path
        self.arrowChanged.emit()
        super().showPopup()

    def hidePopup(self):
        super().hidePopup()
        self._arrow_path = self._arrow_down_path
        self.arrowChanged.emit()


class SubtitleGenerationDialog(QWidget):
    generateRequested = Signal(object)
    canceled = Signal()

    AUDIO_LANGUAGE_OPTIONS = [
        ("auto", "Auto detect"),
        ("en", "English"),
        ("de", "German"),
        ("ru", "Russian"),
        ("uk", "Ukrainian"),
        ("fr", "French"),
        ("es", "Spanish"),
        ("it", "Italian"),
        ("pt", "Portuguese"),
        ("pl", "Polish"),
        ("tr", "Turkish"),
        ("ja", "Japanese"),
        ("ko", "Korean"),
        ("zh", "Chinese"),
    ]
    DEVICE_OPTIONS = [
        ("auto", "Auto"),
        ("cpu", "CPU"),
        ("cuda", "CUDA"),
    ]
    MODEL_OPTIONS = ["tiny", "base", "small", "medium", "large-v3"]
    FORMAT_OPTIONS = ["srt", "vtt"]

    def __init__(
        self,
        theme_color: ThemeState,
        metrics: Metrics,
        media_path: str = "",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowFlag(Qt.Dialog, True)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setWindowModality(Qt.WindowModal)
        self.setWindowTitle("Generate Subtitles")

        self._theme_color = theme_color
        self._metrics = metrics
        self._media_path = media_path
        self._audio_tracks: list[tuple[int | None, str]] = []

        self._init_constants()
        self._build_ui()
        self._populate_static_options()
        self.set_audio_tracks([])
        self.set_media_path(media_path)
        self._apply_fonts()
        self.apply_theme(theme_color)
        self.setFixedSize(self._metrics.subtitle_dialog_width, self._metrics.subtitle_dialog_height)

    def _init_constants(self):
        self.icon_size = max(1, self._metrics.icon_size)
        self.gap = max(1, self.icon_size // 2)
        self.label_width = max(1, int(self.icon_size * 6.2))
        self.button_width = max(1, int(self.icon_size * 5))
        self.button_height = max(1, int(self.icon_size * 1.5))

    def _build_ui(self):
        self.media_label = QLabel("Current media:", self)
        self.media_value_label = QLabel("", self)
        self.media_value_label.setWordWrap(True)

        self.audio_track_label = QLabel("Audio track", self)
        self.audio_track_combo = ArrowComboBox(self)

        self.audio_language_label = QLabel("Audio language", self)
        self.audio_language_combo = ArrowComboBox(self)

        self.device_label = QLabel("Device", self)
        self.device_combo = ArrowComboBox(self)

        self.model_label = QLabel("Whisper model", self)
        self.model_combo = ArrowComboBox(self)

        self.output_format_label = QLabel("Output format", self)
        self.output_format_combo = ArrowComboBox(self)
        self.output_format_combo.currentIndexChanged.connect(self._sync_output_path_extension)

        self.output_path_label = QLabel("Output path", self)
        self.output_path_input = QLineEdit(self)
        self.output_path_browse_button = QPushButton("Browse", self)
        self.output_path_browse_button.clicked.connect(self._choose_output_path)

        self.auto_open_checkbox = QCheckBox("Open subtitles after generation", self)
        self.auto_open_checkbox.setChecked(True)

        self.generate_button = QPushButton("Generate", self)
        self.generate_button.clicked.connect(self._emit_generate_requested)

        self.close_button = QPushButton("Close", self)
        self.close_button.clicked.connect(self._handle_close_clicked)

        for combo in self.combo_boxes:
            combo.arrowChanged.connect(self._refresh_combo_styles)

    def _populate_static_options(self):
        for language_code, language_label in self.AUDIO_LANGUAGE_OPTIONS:
            self.audio_language_combo.addItem(language_label, language_code)

        for device_code, device_label in self.DEVICE_OPTIONS:
            self.device_combo.addItem(device_label, device_code)

        for model in self.MODEL_OPTIONS:
            self.model_combo.addItem(model, model)
        self.model_combo.setCurrentText("small")

        for subtitle_format in self.FORMAT_OPTIONS:
            self.output_format_combo.addItem(subtitle_format.upper(), subtitle_format)

    def set_media_path(self, media_path: str):
        self._media_path = media_path or ""
        self.media_value_label.setText(self._media_path if self._media_path else "No media selected")
        if not self.output_path_input.text().strip():
            self.output_path_input.setText(self._default_output_path())

    def set_audio_tracks(self, tracks: list[tuple[int | None, str]]):
        self._audio_tracks = list(tracks)
        self.audio_track_combo.clear()

        if not self._audio_tracks:
            self.audio_track_combo.addItem("Default track", None)
            return

        for track_id, title in self._audio_tracks:
            self.audio_track_combo.addItem(title, None if track_id is None else int(track_id))

    def set_selected_audio_track(self, track_id: int | None):
        if track_id is None:
            self.audio_track_combo.setCurrentIndex(0)
            return

        for index in range(self.audio_track_combo.count()):
            if self.audio_track_combo.itemData(index) == int(track_id):
                self.audio_track_combo.setCurrentIndex(index)
                return

    def apply_metrics(self, metrics: Metrics):
        self._metrics = metrics
        self._init_constants()
        self._apply_fonts()
        self.setFixedSize(self._metrics.subtitle_dialog_width, self._metrics.subtitle_dialog_height)
        self.updateGeometry()
        self.update()

    def apply_theme(self, theme_color: ThemeState):
        self._theme_color = theme_color

        panel_bg = QColor(*theme_color.get("panel_bg_color"))
        text_color = QColor(*theme_color.get("text_color"))
        accent = QColor(*theme_color.get("progress_bar_color_active"))

        palette = self.palette()
        palette.setColor(QPalette.WindowText, text_color)
        self.setPalette(palette)
        self.setAutoFillBackground(False)

        self._apply_label_palette(text_color)

        for combo in self.combo_boxes:
            combo.setStyleSheet(self._build_combo_style(combo, panel_bg, text_color, accent))

        self.output_path_input.setStyleSheet(self._build_line_edit_style())
        self.output_path_browse_button.setStyleSheet("")
        self.generate_button.setStyleSheet("")
        self.close_button.setStyleSheet("")
        checkbox_style = self._build_checkbox_style(text_color)
        self.auto_open_checkbox.setStyleSheet(checkbox_style)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        width = self.width()
        height = self.height()

        field_x = self.gap + self.label_width + self.gap
        row_width = width - field_x - self.gap
        path_input_width = max(1, row_width - self.button_width - self.gap)
        browse_x = field_x + path_input_width + self.gap
        y = self.icon_size

        self.media_label.setGeometry(self.gap, y, self.label_width, self.button_height)
        self.media_value_label.setGeometry(field_x, y, row_width, self.button_height)
        y += self.button_height + self.icon_size

        self.audio_track_label.setGeometry(self.gap, y, self.label_width, self.button_height)
        self.audio_track_combo.setGeometry(field_x, y, row_width, self.button_height)
        y += self.button_height + self.gap

        self.audio_language_label.setGeometry(self.gap, y, self.label_width, self.button_height)
        self.audio_language_combo.setGeometry(field_x, y, row_width, self.button_height)
        y += self.button_height + self.gap

        self.device_label.setGeometry(self.gap, y, self.label_width, self.button_height)
        self.device_combo.setGeometry(field_x, y, row_width, self.button_height)
        y += self.button_height + self.gap

        self.model_label.setGeometry(self.gap, y, self.label_width, self.button_height)
        self.model_combo.setGeometry(field_x, y, row_width, self.button_height)
        y += self.button_height + self.gap

        self.output_format_label.setGeometry(self.gap, y, self.label_width, self.button_height)
        self.output_format_combo.setGeometry(field_x, y, row_width, self.button_height)
        y += self.button_height + self.gap

        self.output_path_label.setGeometry(self.gap, y, self.label_width, self.button_height)
        self.output_path_input.setGeometry(field_x, y, path_input_width, self.button_height)
        self.output_path_browse_button.setGeometry(browse_x, y, self.button_width, self.button_height)
        y += self.button_height + self.gap

        self.auto_open_checkbox.setGeometry(field_x, y, row_width, self.button_height)

        button_y = height - self.gap - self.button_height
        close_button_x = width - self.gap - self.button_width
        generate_button_x = close_button_x - self.button_width - self.icon_size
        self.close_button.setGeometry(close_button_x, button_y, self.button_width, self.button_height)
        self.generate_button.setGeometry(generate_button_x, button_y, self.button_width, self.button_height)

    def get_result(self) -> SubtitleGenerationDialogResult:
        audio_track_id = self.audio_track_combo.currentData()
        audio_language = self.audio_language_combo.currentData()
        device = self.device_combo.currentData()
        if audio_language == "auto":
            audio_language = None
        if device == "auto":
            device = None

        return SubtitleGenerationDialogResult(
            audio_track_id=int(audio_track_id) if audio_track_id is not None else None,
            audio_language=audio_language,
            device=device,
            model_size=str(self.model_combo.currentData()),
            output_format=str(self.output_format_combo.currentData()),
            output_path=self.output_path_input.text().strip(),
            auto_open_after_generation=self.auto_open_checkbox.isChecked(),
        )

    def _apply_fonts(self):
        normal_font = self.font()
        normal_font.setPixelSize(self._metrics.font_size)

        for widget in self.font_widgets:
            widget.setFont(normal_font)

    def _apply_label_palette(self, text_color: QColor):
        for label in self.text_labels:
            label_palette = label.palette()
            label_palette.setColor(QPalette.WindowText, text_color)
            label.setPalette(label_palette)

    @property
    def combo_boxes(self) -> tuple[ArrowComboBox, ...]:
        return (
            self.audio_track_combo,
            self.audio_language_combo,
            self.device_combo,
            self.model_combo,
            self.output_format_combo,
        )

    @property
    def text_labels(self) -> tuple[QLabel, ...]:
        return (
            self.media_label,
            self.media_value_label,
            self.audio_track_label,
            self.audio_language_label,
            self.device_label,
            self.model_label,
            self.output_format_label,
            self.output_path_label,
        )

    @property
    def font_widgets(self) -> tuple[QWidget, ...]:
        return (
            self.media_label,
            self.media_value_label,
            self.audio_track_label,
            self.audio_track_combo,
            self.audio_language_label,
            self.audio_language_combo,
            self.device_label,
            self.device_combo,
            self.model_label,
            self.model_combo,
            self.output_format_label,
            self.output_format_combo,
            self.output_path_label,
            self.output_path_input,
            self.output_path_browse_button,
            self.auto_open_checkbox,
            self.generate_button,
            self.close_button,
        )

    def _build_combo_style(self, combo: ArrowComboBox, panel_bg: QColor, text_color: QColor, accent: QColor) -> str:
        return f"""
            QComboBox {{
                padding: 0px 8px;
            }}
            QComboBox::drop-down {{
                min-width: {self.icon_size}px;
                border: 0px;
            }}
            QComboBox::down-arrow {{
                min-width: {self.icon_size}px;
                image: url("{combo.arrow_path}");
            }}
            QComboBox QAbstractItemView {{
                background-color: rgb({panel_bg.red()}, {panel_bg.green()}, {panel_bg.blue()});
                color: rgb({text_color.red()}, {text_color.green()}, {text_color.blue()});
                selection-background-color: rgb({accent.red()}, {accent.green()}, {accent.blue()});
            }}
        """

    def _build_line_edit_style(self) -> str:
        return f"""
            QLineEdit {{
                padding: 0px 8px;
            }}
        """

    def _refresh_combo_styles(self):
        panel_bg = QColor(*self._theme_color.get("panel_bg_color"))
        text_color = QColor(*self._theme_color.get("text_color"))
        accent = QColor(*self._theme_color.get("progress_bar_color_active"))
        for combo in self.combo_boxes:
            combo.setStyleSheet(self._build_combo_style(combo, panel_bg, text_color, accent))

    def _build_checkbox_style(self, text_color: QColor) -> str:
        return f"""
            QCheckBox {{
                color: rgb({text_color.red()}, {text_color.green()}, {text_color.blue()});
            }}
            QCheckBox::indicator {{
                width: {self._metrics.font_size}px;
                height: {self._metrics.font_size}px;
            }}
        """

    def _default_output_path(self) -> str:
        if not self._media_path:
            extension = self.output_format_combo.currentData() or "srt"
            return f"subtitles.{extension}"

        base_path, _ = os.path.splitext(self._media_path)
        extension = self.output_format_combo.currentData() or "srt"
        return f"{base_path}.{extension}"

    def _sync_output_path_extension(self):
        current_path = self.output_path_input.text().strip()
        if not current_path:
            self.output_path_input.setText(self._default_output_path())
            return

        current_extension = str(self.output_format_combo.currentData() or "srt")
        base_path, _ = os.path.splitext(current_path)
        self.output_path_input.setText(f"{base_path}.{current_extension}")

    def _choose_output_path(self):
        extension = str(self.output_format_combo.currentData() or "srt")
        if self.output_path_input.text().strip():
            initial_path = self.output_path_input.text().strip()
        else:
            initial_path = self._default_output_path()

        chosen_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Subtitle As",
            initial_path,
            f"{extension.upper()} Files (*.{extension});;All Files (*)",
        )
        if not chosen_path:
            return

        if not chosen_path.lower().endswith(f".{extension.lower()}"):
            chosen_path = f"{chosen_path}.{extension}"
        self.output_path_input.setText(chosen_path)

    def _emit_generate_requested(self):
        self.generateRequested.emit(self.get_result())

    def _handle_close_clicked(self):
        self.canceled.emit()
        self.close()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(res_path("assets/logo.ico")))

    preview_host = QWidget()
    dialog = SubtitleGenerationDialog(
        theme_color=ThemeState(),
        metrics=get_metrics(preview_host),
        media_path=r"C:\Media\Example Movie.mkv",
    )
    dialog.set_audio_tracks(
        [
            (1, "Audio 1 - English 5.1"),
            (2, "Audio 2 - German Stereo"),
            (3, "Audio 3 - Commentary"),
        ]
    )
    dialog.show()

    sys.exit(app.exec())
