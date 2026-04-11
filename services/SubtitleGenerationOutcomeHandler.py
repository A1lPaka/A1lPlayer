from __future__ import annotations

from enum import Enum, auto

from PySide6.QtWidgets import QWidget

from ui.MessageBoxService import (
    show_cuda_runtime_install_canceled,
    show_cuda_runtime_install_failed,
    show_subtitle_auto_load_failed,
    show_subtitle_created,
    show_subtitle_created_not_loaded_due_to_context_change,
    show_subtitle_generation_canceled,
    show_subtitle_generation_failed,
)


class SubtitleAutoOpenOutcome(Enum):
    LOADED = auto()
    CONTEXT_CHANGED = auto()
    LOAD_FAILED = auto()


class SubtitleGenerationOutcomeHandler:
    def __init__(self, parent: QWidget):
        self._parent = parent

    def show_generation_success(self, output_path: str, auto_open_outcome: SubtitleAutoOpenOutcome):
        if auto_open_outcome == SubtitleAutoOpenOutcome.CONTEXT_CHANGED:
            show_subtitle_created_not_loaded_due_to_context_change(self._parent, output_path)
            return

        if auto_open_outcome == SubtitleAutoOpenOutcome.LOAD_FAILED:
            show_subtitle_auto_load_failed(self._parent, output_path)
            return

        show_subtitle_created(self._parent, output_path)

    def show_generation_failed(self, error_text: str):
        show_subtitle_generation_failed(self._parent, error_text)

    def show_generation_canceled(self):
        show_subtitle_generation_canceled(self._parent)

    def show_cuda_install_failed(self, error_text: str):
        show_cuda_runtime_install_failed(self._parent, error_text)

    def show_cuda_install_canceled(self):
        show_cuda_runtime_install_canceled(self._parent)
