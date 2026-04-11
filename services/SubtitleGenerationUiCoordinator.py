from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QWidget

from models.ThemeColor import ThemeState
from ui.SubtitleGenerationDialog import (
    SubtitleGenerationDialog,
    SubtitleGenerationDialogResult,
)
from ui.SubtitleProgressDialog import SubtitleProgressDialog
from utils import get_metrics


class SubtitleGenerationUiCoordinator(QObject):
    _SUBTITLE_CANCEL_PENDING_STATUS = "Cancellation requested..."
    _SUBTITLE_CANCEL_PENDING_DETAILS = (
        "Stopping the subtitle generation subprocess.\n"
        "The active transcription job is being terminated outside the GUI process."
    )
    _CUDA_CANCEL_PENDING_DETAILS = (
        "Stopping the GPU runtime installation.\n"
        "If the installer subsystem is already finishing a step, this may take a moment."
    )

    def __init__(
        self,
        parent: QWidget,
        *,
        theme_color_getter: Callable[[], ThemeState],
    ):
        super().__init__(parent)
        self._parent = parent
        self._theme_color_getter = theme_color_getter
        self._generation_dialog: SubtitleGenerationDialog | None = None
        self._progress_dialog: SubtitleProgressDialog | None = None

    def open_generation_dialog(
        self,
        media_path: str,
        audio_tracks: list[tuple[int | None, str]],
        *,
        on_generate: Callable[[SubtitleGenerationDialogResult], None],
        on_cancel: Callable[[], None] | None = None,
    ):
        dialog = SubtitleGenerationDialog(
            theme_color=self._theme_color_getter(),
            metrics=get_metrics(self._parent),
            media_path=media_path,
            parent=self._parent,
        )
        dialog.set_audio_tracks(audio_tracks)
        dialog.set_selected_audio_track(None)
        dialog.generateRequested.connect(on_generate)
        if on_cancel is not None:
            dialog.canceled.connect(on_cancel)
        dialog.canceled.connect(self._clear_generation_dialog_reference)
        dialog.destroyed.connect(self._clear_generation_dialog_reference)

        self._generation_dialog = dialog
        self._show_and_focus(dialog)

    def focus_active_dialog(self):
        if self._progress_dialog is not None:
            self._show_and_focus(self._progress_dialog)
            return
        if self._generation_dialog is not None:
            self._show_and_focus(self._generation_dialog)

    def open_generation_progress(
        self,
        options: SubtitleGenerationDialogResult,
        *,
        on_cancel: Callable[[], None],
    ):
        self._close_generation_dialog(delete_later=True)
        progress_dialog = self._create_progress_dialog(on_cancel=on_cancel)
        progress_dialog.set_status("Preparing subtitle generation...")
        progress_dialog.set_details(
            f"Device: {options.device or 'Auto'}\n"
            f"Model: {options.model_size}\n"
            f"Language: {options.audio_language or 'Auto detect'}\n"
            f"Output: {options.output_path}"
        )
        progress_dialog.set_cancel_enabled(True, "Cancel")

    def open_cuda_install_progress(
        self,
        missing_packages: list[str],
        *,
        on_cancel: Callable[[], None],
    ):
        self._close_generation_dialog(delete_later=True)
        progress_dialog = self._create_progress_dialog(on_cancel=on_cancel)
        progress_dialog.set_status("Installing GPU runtime...")
        progress_dialog.set_indeterminate(True)
        progress_dialog.set_details(
            "Preparing GPU runtime installer...\n"
            "The installer subsystem will resolve the configured source automatically.\n\n"
            "Packages:\n"
            + "\n".join(missing_packages)
        )
        progress_dialog.set_cancel_enabled(True, "Cancel")

    def show_subtitle_cancel_pending(self):
        if self._progress_dialog is None:
            return
        self._progress_dialog.set_indeterminate(True)
        self._progress_dialog.set_status(self._SUBTITLE_CANCEL_PENDING_STATUS)
        self._progress_dialog.set_details(self._SUBTITLE_CANCEL_PENDING_DETAILS)
        self._progress_dialog.set_cancel_enabled(False, "Cancelling...")

    def show_cuda_install_cancel_pending(self):
        if self._progress_dialog is None:
            return
        self._progress_dialog.set_indeterminate(True)
        self._progress_dialog.set_status(self._SUBTITLE_CANCEL_PENDING_STATUS)
        self._progress_dialog.set_details(self._CUDA_CANCEL_PENDING_DETAILS)
        self._progress_dialog.set_cancel_enabled(False, "Cancelling...")

    def close_generation_dialog(self):
        self._close_generation_dialog(delete_later=False)

    def close_progress_dialog(self):
        if self._progress_dialog is None:
            return
        self._progress_dialog.close()
        self._progress_dialog = None

    def update_progress_status(self, text: str):
        if self._progress_dialog is not None:
            self._progress_dialog.set_status(text)

    def update_progress(self, value: int):
        if self._progress_dialog is not None:
            self._progress_dialog.set_progress(value)

    def update_progress_details(self, text: str):
        if self._progress_dialog is not None:
            self._progress_dialog.set_details(text)

    def _create_progress_dialog(
        self,
        *,
        on_cancel: Callable[[], None],
    ) -> SubtitleProgressDialog:
        progress_dialog = SubtitleProgressDialog(
            theme_color=self._theme_color_getter(),
            metrics=get_metrics(self._parent),
            parent=self._parent,
        )
        progress_dialog.cancelRequested.connect(on_cancel)
        progress_dialog.destroyed.connect(self._clear_progress_dialog_reference)
        self._progress_dialog = progress_dialog
        self._show_and_focus(progress_dialog)
        return progress_dialog

    def _show_and_focus(self, widget: QWidget):
        widget.show()
        widget.raise_()
        widget.activateWindow()

    def _close_generation_dialog(self, *, delete_later: bool):
        if self._generation_dialog is None:
            return

        dialog = self._generation_dialog
        dialog.hide()
        self._generation_dialog = None
        if delete_later:
            dialog.deleteLater()
            return
        dialog.close()

    def _clear_generation_dialog_reference(self, *_args):
        self._generation_dialog = None

    def _clear_progress_dialog_reference(self, *_args):
        self._progress_dialog = None
