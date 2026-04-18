from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QWidget

from models import SubtitleGenerationDialogResult
from models.ThemeColor import ThemeState
from ui.SubtitleGenerationDialog import SubtitleGenerationDialog
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
        dialog.generateRequested.connect(on_generate)
        if on_cancel is not None:
            dialog.canceled.connect(on_cancel)
        dialog.canceled.connect(self._clear_generation_dialog_reference)
        dialog.destroyed.connect(self._clear_generation_dialog_reference)

        self._generation_dialog = dialog
        self._show_and_focus(dialog)

    def set_generation_dialog_audio_tracks_loading(self):
        if self._generation_dialog is None:
            return
        self._generation_dialog.set_audio_tracks_loading()

    def apply_generation_dialog_audio_tracks(
        self,
        audio_tracks: list[tuple[int | None, str]],
        *,
        selected_track_id: int | None = None,
        selector_enabled: bool,
        generate_enabled: bool,
    ):
        if self._generation_dialog is None:
            return
        self._generation_dialog.set_audio_tracks(audio_tracks)
        self._generation_dialog.set_selected_audio_track(selected_track_id)
        self._generation_dialog.set_audio_track_selector_enabled(selector_enabled)
        self._generation_dialog.set_generate_enabled(generate_enabled)

    def has_generation_dialog(self) -> bool:
        return self._generation_dialog is not None

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
        self._replace_generation_dialog_with_progress(
            on_cancel=on_cancel,
            configure_progress_dialog=lambda progress_dialog: self._configure_generation_progress_dialog(
                progress_dialog,
                options,
            ),
        )

    def open_cuda_install_progress(
        self,
        missing_packages: list[str],
        *,
        on_cancel: Callable[[], None],
    ):
        self._replace_generation_dialog_with_progress(
            on_cancel=on_cancel,
            configure_progress_dialog=lambda progress_dialog: self._configure_cuda_install_progress_dialog(
                progress_dialog,
                missing_packages,
            ),
        )

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
        return progress_dialog

    def _show_and_focus(self, widget: QWidget):
        widget.show()
        widget.raise_()
        widget.activateWindow()

    def _replace_generation_dialog_with_progress(
        self,
        *,
        on_cancel: Callable[[], None],
        configure_progress_dialog: Callable[[SubtitleProgressDialog], None],
    ):
        dialog = self._generation_dialog
        if dialog is not None:
            dialog.hide()
            self._generation_dialog = None

        progress_dialog = self._create_progress_dialog(on_cancel=on_cancel)
        configure_progress_dialog(progress_dialog)
        self._show_and_focus(progress_dialog)

        if dialog is not None:
            QTimer.singleShot(0, dialog.deleteLater)

    def _configure_generation_progress_dialog(
        self,
        progress_dialog: SubtitleProgressDialog,
        options: SubtitleGenerationDialogResult,
    ):
        progress_dialog.set_status("Preparing subtitle generation...")
        progress_dialog.set_details(
            f"Device: {options.device or 'Auto'}\n"
            f"Model: {options.model_size}\n"
            f"Language: {options.audio_language or 'Auto detect'}\n"
            f"Output: {options.output_path}"
        )
        progress_dialog.set_cancel_enabled(True, "Cancel")

    def _configure_cuda_install_progress_dialog(
        self,
        progress_dialog: SubtitleProgressDialog,
        missing_packages: list[str],
    ):
        progress_dialog.set_status("Installing GPU runtime...")
        progress_dialog.set_indeterminate(True)
        progress_dialog.set_details(
            "Preparing GPU runtime installer...\n"
            "The installer subsystem will resolve the configured source automatically.\n\n"
            "Packages:\n"
            + "\n".join(missing_packages)
        )
        progress_dialog.set_cancel_enabled(True, "Cancel")

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
