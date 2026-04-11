from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    project_root = Path(__file__).resolve().parent.parent
    project_root_str = str(project_root)
    if project_root_str not in sys.path:
        sys.path.insert(0, project_root_str)

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QProgressBar, QWidget

from models.ThemeColor import ThemeState
from utils import Metrics, get_metrics, res_path


class SubtitleProgressDialog(QWidget):
    cancelRequested = Signal()

    def __init__(self, theme_color: ThemeState, metrics: Metrics, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowFlag(Qt.Dialog, True)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setWindowModality(Qt.WindowModal)
        self.setWindowTitle("Generating Subtitles")

        self._theme_color = theme_color
        self._metrics = metrics
        self._status_text = "Preparing subtitle generation..."
        self._init_constants()
        self._build_ui()
        self._apply_fonts()
        self.apply_theme(theme_color)
        self.setFixedSize(self._metrics.subtitle_progress_dialog_width, self._metrics.subtitle_progress_dialog_height)

    def _init_constants(self):
        self.icon_size = max(1, self._metrics.icon_size)
        self.gap = max(1, self.icon_size // 2)
        self.button_width = max(1, int(self.icon_size * 5.2))
        self.button_height = max(1, int(self.icon_size * 1.5))
        self.status_height = max(1, int(self.icon_size * 2.0))
        self.progress_height = max(1, int(self.icon_size * 1.2))
        self.details_height = max(1, int(self.icon_size * 2.1))

    def _build_ui(self):
        self.status_label = QLabel(self._status_text, self)
        self.status_label.setWordWrap(True)
        self.status_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)

        self.details_label = QLabel("Model loading and transcription progress will appear here.", self)
        self.details_label.setWordWrap(True)
        self.details_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)

        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.clicked.connect(self.cancelRequested.emit)

    def apply_metrics(self, metrics: Metrics):
        self._metrics = metrics
        self._init_constants()
        self._apply_fonts()
        self.setFixedSize(self._metrics.subtitle_progress_dialog_width, self._metrics.subtitle_progress_dialog_height)
        self.updateGeometry()
        self.update()

    def apply_theme(self, theme_color: ThemeState):
        self._theme_color = theme_color

        panel_bg = QColor(*theme_color.get("panel_bg_color"))
        separator = QColor(*theme_color.get("panel_bg_color_separator"))
        text_color = QColor(*theme_color.get("text_color"))
        accent = QColor(*theme_color.get("progress_bar_color_active"))

        palette = self.palette()
        palette.setColor(QPalette.WindowText, text_color)
        self.setPalette(palette)
        self.setAutoFillBackground(False)

        self._apply_label_palette(text_color)

        self.progress_bar.setStyleSheet(
            f"""
            QProgressBar {{
                background-color: rgb({panel_bg.red()}, {panel_bg.green()}, {panel_bg.blue()});
                color: rgb({text_color.red()}, {text_color.green()}, {text_color.blue()});
                border: 1px solid rgb({separator.red()}, {separator.green()}, {separator.blue()});
                text-align: center;
                border-radius: 2px;
            }}
            QProgressBar::chunk {{
                background-color: rgb({accent.red()}, {accent.green()}, {accent.blue()});
                border-radius: 2px;
            }}
            """
        )
        self.cancel_button.setStyleSheet("")

    def resizeEvent(self, event):
        super().resizeEvent(event)

        width = self.width()
        y = self.gap

        self.status_label.setGeometry(self.gap, y, width - 2 * self.gap, self.status_height)
        y += self.status_height + self.gap

        self.progress_bar.setGeometry(self.gap, y, width - 2 * self.gap, self.progress_height)
        y += self.progress_height + self.gap

        button_y = self.height() - self.gap - self.button_height
        details_height = max(self.details_height, button_y - y - self.gap)
        self.details_label.setGeometry(self.gap, y, width - 2 * self.gap, details_height)
        self.cancel_button.setGeometry(width - self.gap - self.button_width, button_y, self.button_width, self.button_height)

    def set_status(self, text: str):
        self._status_text = str(text)
        self._update_status_label()

    def set_details(self, text: str):
        self.details_label.setText(text)

    def set_cancel_enabled(self, enabled: bool, button_text: str | None = None):
        self.cancel_button.setEnabled(bool(enabled))
        self.cancel_button.setText(button_text or "Cancel")

    def set_progress(self, value: int):
        bounded_value = max(0, min(100, int(value)))
        if self.progress_bar.minimum() == 0 and self.progress_bar.maximum() == 0:
            self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(bounded_value)
        self._update_status_label()

    def set_indeterminate(self, active: bool):
        if active:
            self.progress_bar.setRange(0, 0)
            self.status_label.setText(self._status_text)
            return
        self.progress_bar.setRange(0, 100)
        if self.progress_bar.value() < 0:
            self.progress_bar.setValue(0)
        self._update_status_label()

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

    def _update_status_label(self):
        if self.progress_bar.minimum() == 0 and self.progress_bar.maximum() == 0:
            self.status_label.setText(self._status_text)
            return
        self.status_label.setText(f"{self._status_text} ({self.progress_bar.value()}%)")

    @property
    def text_labels(self) -> tuple[QLabel, ...]:
        return self.status_label, self.details_label

    @property
    def font_widgets(self) -> tuple[QWidget, ...]:
        return (
            self.status_label,
            self.progress_bar,
            self.details_label,
            self.cancel_button,
        )


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(res_path("assets/logo.ico")))

    preview_host = QWidget()
    dialog = SubtitleProgressDialog(
        theme_color=ThemeState(),
        metrics=get_metrics(preview_host),
    )
    dialog.set_status("Transcribing audio track 2...")
    dialog.set_details("Model: small. Language: Auto detect. Output: C:\\Media\\Example Movie.srt")
    dialog.set_progress(42)
    dialog.show()

    sys.exit(app.exec())
