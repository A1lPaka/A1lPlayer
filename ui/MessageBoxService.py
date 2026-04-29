from __future__ import annotations

from typing import Literal

from PySide6.QtWidgets import QMessageBox, QWidget

from utils import format_ms


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
        "Subtitle generation can continue with the current/default audio track, "
        "but alternate stream selection is unavailable until this is fixed.",
    )


def show_audio_stream_inspection_failed(parent: QWidget, reason: str) -> None:
    QMessageBox.warning(
        parent,
        "Generate Subtitle",
        "Subtitle generation cannot start because audio streams could not be inspected.\n\n"
        f"Reason:\n{reason}",
    )


def show_audio_streams_still_loading(parent: QWidget) -> None:
    QMessageBox.information(
        parent,
        "Generate Subtitle",
        "Audio tracks are still loading for this media file.\n\n"
        "Please wait a moment and try again.",
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


def prompt_whisper_model_install_choice(
    parent: QWidget,
    model_size: str,
) -> Literal["download", "fallback", "cancel"]:
    message_box = QMessageBox(parent)
    message_box.setWindowTitle("Whisper Model Required")
    message_box.setIcon(QMessageBox.Icon.Question)
    message_box.setText(f"Whisper model '{model_size}' is not installed.")
    message_box.setInformativeText(
        "Download and install this model from the internet now?\n\n"
        "This is usually needed only once."
    )
    download_button = message_box.addButton("Download", QMessageBox.ButtonRole.AcceptRole)
    fallback_button = message_box.addButton("Use installed smaller model", QMessageBox.ButtonRole.ActionRole)
    cancel_button = message_box.addButton(QMessageBox.StandardButton.Cancel)
    message_box.setDefaultButton(download_button)
    message_box.exec()

    clicked_button = message_box.clickedButton()
    if clicked_button == download_button:
        return "download"
    if clicked_button == fallback_button:
        return "fallback"
    if clicked_button == cancel_button:
        return "cancel"
    return "cancel"


def prompt_whisper_model_fallback_choice(
    parent: QWidget,
    requested_model_size: str,
    fallback_model_size: str | None,
) -> Literal["fallback", "cancel"]:
    if not fallback_model_size:
        QMessageBox.information(
            parent,
            "Whisper Model Required",
            f"Model '{requested_model_size}' is not installed and no smaller installed model is available.",
        )
        return "cancel"

    answer = QMessageBox.question(
        parent,
        "Use Installed Model",
        f"Use installed smaller model '{fallback_model_size}' instead of '{requested_model_size}'?",
    )
    if answer == QMessageBox.StandardButton.Yes:
        return "fallback"
    return "cancel"


def prompt_whisper_model_install_retry(
    parent: QWidget,
    model_size: str,
    error_text: str,
) -> Literal["retry", "cancel"]:
    message_box = QMessageBox(parent)
    message_box.setWindowTitle("Whisper Model")
    message_box.setIcon(QMessageBox.Icon.Warning)
    message_box.setText(f"Failed to install Whisper model '{model_size}'.")
    message_box.setInformativeText("Try downloading it again?")
    if error_text:
        message_box.setDetailedText(error_text)
    retry_button = message_box.addButton("Retry", QMessageBox.ButtonRole.AcceptRole)
    cancel_button = message_box.addButton(QMessageBox.StandardButton.Cancel)
    message_box.setDefaultButton(retry_button)
    message_box.exec()
    if message_box.clickedButton() == retry_button:
        return "retry"
    if message_box.clickedButton() == cancel_button:
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


def show_whisper_model_install_canceled(parent: QWidget) -> None:
    QMessageBox.information(
        parent,
        "Whisper Model",
        "Whisper model installation was canceled.",
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


def show_no_supported_media_found(parent: QWidget, path: str | None) -> None:
    message = "No supported media files were found."
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
        f"Last position: {format_ms(position_ms)}\n\n"
        "Continue from where you left off?"
    )
    message_box.setIcon(QMessageBox.Icon.NoIcon)
    message_box.setStandardButtons(
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    )
    return message_box.exec() == QMessageBox.StandardButton.Yes
