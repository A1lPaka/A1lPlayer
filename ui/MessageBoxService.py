from __future__ import annotations

from typing import Literal

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox, QWidget

from utils import _format_ms


def confirm_force_close_background_tasks(parent: QWidget) -> bool:
    message_box = QMessageBox(parent)
    message_box.setWindowTitle("Closing Application")
    message_box.setIcon(QMessageBox.Icon.Warning)
    message_box.setText("Background subtitle tasks are still stopping.")
    message_box.setInformativeText(
        "Wait for them to finish, or force close the application now."
    )
    wait_button = message_box.addButton("Wait", QMessageBox.ButtonRole.RejectRole)
    force_close_button = message_box.addButton(
        "Force close",
        QMessageBox.ButtonRole.DestructiveRole,
    )
    message_box.setDefaultButton(wait_button)
    message_box.exec()
    return message_box.clickedButton() is force_close_button


def prompt_force_close_background_tasks(
    parent: QWidget,
    *,
    on_wait,
    on_force_close,
) -> QMessageBox:
    message_box = QMessageBox(parent)
    message_box.setWindowTitle("Closing Application")
    message_box.setIcon(QMessageBox.Icon.Warning)
    message_box.setText("Background subtitle tasks are still stopping.")
    message_box.setInformativeText(
        "Wait for them to finish, or force close the application now."
    )
    wait_button = message_box.addButton("Wait", QMessageBox.ButtonRole.RejectRole)
    force_close_button = message_box.addButton(
        "Force close",
        QMessageBox.ButtonRole.DestructiveRole,
    )
    message_box.setDefaultButton(wait_button)
    message_box.setWindowModality(Qt.WindowModality.ApplicationModal)
    message_box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

    def _handle_button_clicked(clicked_button):
        if clicked_button is force_close_button:
            on_force_close()
            return
        on_wait()

    message_box.buttonClicked.connect(_handle_button_clicked)
    message_box.open()
    return message_box


def show_force_close_still_running(parent: QWidget) -> None:
    QMessageBox.warning(
        parent,
        "Closing Application",
        "Background subtitle tasks are still stopping.\n\n"
        "The application cannot close safely yet. Please wait a moment and try again.",
    )


def show_playback_error(parent: QWidget, message: str, path: str) -> None:
    details = path or "Unknown file"
    QMessageBox.warning(
        parent,
        "Playback Error",
        f"{message}\n\n{details}",
    )


def show_subtitle_generation_already_running(parent: QWidget) -> None:
    QMessageBox.information(
        parent,
        "Generate Subtitle",
        "Subtitle generation is already running.",
    )


def show_no_audio_streams_found(parent: QWidget) -> None:
    QMessageBox.warning(
        parent,
        "Generate Subtitle",
        "No audio streams were found in this media file.",
    )


def show_audio_stream_no_longer_available(parent: QWidget) -> None:
    QMessageBox.warning(
        parent,
        "Generate Subtitle",
        "The selected audio stream is no longer available for this media file.",
    )


def show_audio_stream_inspection_warning(parent: QWidget, reason: str) -> None:
    QMessageBox.warning(
        parent,
        "Generate Subtitle",
        "Audio streams could not be inspected for this media file.\n\n"
        f"Reason:\n{reason}\n\n"
        "The dialog can stay open, but subtitle generation will be blocked until this is fixed.",
    )


def show_audio_stream_inspection_failed(parent: QWidget, reason: str) -> None:
    QMessageBox.warning(
        parent,
        "Generate Subtitle",
        "Subtitle generation cannot start because audio streams could not be inspected.\n\n"
        f"Reason:\n{reason}",
    )


def prompt_cuda_runtime_choice(
    parent: QWidget,
    missing_packages: list[str],
) -> Literal["download", "cpu", "cancel"]:
    message_box = QMessageBox(parent)
    message_box.setWindowTitle("CUDA Runtime Required")
    message_box.setIcon(QMessageBox.Icon.Question)
    message_box.setText(
        "CUDA was selected, but the required GPU runtime is not installed."
    )
    message_box.setInformativeText(
        "Download it now?\n\n"
        "This is usually needed only once and may download about 1.3 GB."
    )
    message_box.setDetailedText("Missing packages:\n" + "\n".join(missing_packages))

    download_button = message_box.addButton(
        "Download",
        QMessageBox.ButtonRole.AcceptRole,
    )
    cpu_button = message_box.addButton("Use CPU", QMessageBox.ButtonRole.ActionRole)
    cancel_button = message_box.addButton(QMessageBox.StandardButton.Cancel)
    message_box.setDefaultButton(download_button)
    message_box.exec()

    clicked_button = message_box.clickedButton()
    if clicked_button == download_button:
        return "download"
    if clicked_button == cpu_button:
        return "cpu"
    if clicked_button == cancel_button:
        return "cancel"
    return "cancel"


def show_choose_output_path_first(parent: QWidget) -> None:
    QMessageBox.warning(
        parent,
        "Generate Subtitle",
        "Choose an output path first.",
    )


def confirm_overwrite_subtitle(parent: QWidget, output_path: str) -> bool:
    answer = QMessageBox.question(
        parent,
        "Overwrite Subtitle",
        f"Subtitle file already exists:\n{output_path}\n\nOverwrite it?",
    )
    return answer == QMessageBox.StandardButton.Yes


def show_subtitle_output_path_unavailable(
    parent: QWidget,
    output_path: str,
    reason: str | None = None,
) -> None:
    message = "The selected output location is not writable."
    if output_path:
        message += f"\n\nPath:\n{output_path}"
    if reason:
        message += f"\n\nReason:\n{reason}"
    QMessageBox.warning(parent, "Generate Subtitle", message)


def show_subtitle_created_not_loaded_due_to_context_change(
    parent: QWidget,
    output_path: str,
) -> None:
    QMessageBox.information(
        parent,
        "Generate Subtitle",
        "Subtitle file created, but it was not loaded automatically.\n\n"
        f"File: {output_path}\n\n"
        "The active media changed while subtitle generation was running.",
    )


def show_subtitle_auto_load_failed(parent: QWidget, output_path: str) -> None:
    QMessageBox.warning(
        parent,
        "Generate Subtitle",
        "Subtitle file created, but automatic loading failed.\n\n"
        f"File: {output_path}\n\n"
        "Current subtitles were kept unchanged.",
    )


def show_subtitle_created(parent: QWidget, output_path: str) -> None:
    QMessageBox.information(
        parent,
        "Generate Subtitle",
        f"Subtitle file created:\n{output_path}",
    )


def show_subtitle_created_with_fallback_name(
    parent: QWidget,
    requested_output_path: str,
    actual_output_path: str,
) -> None:
    QMessageBox.information(
        parent,
        "Generate Subtitle",
        "The original subtitle file could not be overwritten because it is currently in use.\n\n"
        f"Requested path:\n{requested_output_path}\n\n"
        f"Created file:\n{actual_output_path}",
    )


def show_subtitle_generation_failed(parent: QWidget, error_text: str) -> None:
    QMessageBox.warning(parent, "Generate Subtitle", error_text)


def show_subtitle_generation_canceled(parent: QWidget) -> None:
    QMessageBox.information(
        parent,
        "Generate Subtitle",
        "Subtitle generation was canceled.",
    )


def show_cuda_runtime_install_failed(parent: QWidget, error_text: str) -> None:
    QMessageBox.warning(parent, "CUDA Runtime", error_text)


def show_cuda_runtime_install_canceled(parent: QWidget) -> None:
    QMessageBox.information(
        parent,
        "CUDA Runtime",
        "GPU runtime installation was canceled.",
    )


def show_open_subtitle_failed(parent: QWidget) -> None:
    QMessageBox.warning(
        parent,
        "Open Subtitle",
        "Failed to load the selected subtitle file.\nCurrent subtitles were kept unchanged.",
    )


def show_media_access_failed(parent: QWidget, path: str | None) -> None:
    message = (
        "The selected file or folder could not be accessed.\n"
        "It may have been removed, disconnected, or requires additional permissions."
    )
    if path:
        message = f"{message}\n\nPath:\n{path}"
    QMessageBox.warning(
        parent,
        "Open Media",
        message,
    )


def confirm_resume_playback(parent: QWidget, path: str, position_ms: int) -> bool:
    message_box = QMessageBox(parent)
    message_box.setWindowTitle("Resume playback")
    message_box.setText(
        f"Resume playback for:\n{path}\n\n"
        f"Last position: {_format_ms(position_ms)}\n\n"
        "Continue from where you left off?"
    )
    message_box.setIcon(QMessageBox.Icon.NoIcon)
    message_box.setStandardButtons(
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    )
    return message_box.exec() == QMessageBox.StandardButton.Yes
