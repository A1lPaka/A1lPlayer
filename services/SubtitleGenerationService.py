import os
from dataclasses import replace

from PySide6.QtCore import QObject, QThread, Slot
from PySide6.QtWidgets import QMessageBox, QWidget

from services.MediaSettingsStore import MediaSettingsStore
from services.SubtitleGenerationWorkers import (
    CudaRuntimeInstallWorker,
    SubtitleGenerationWorker,
)
from services.SubtitleMaker import get_missing_windows_cuda_runtime_packages
from ui.SubtitleGenerationDialog import (
    SubtitleGenerationDialog,
    SubtitleGenerationDialogResult,
)
from ui.SubtitleProgressDialog import SubtitleProgressDialog
from ui.PlayerWindow import PlayerWindow
from utils import get_metrics


class SubtitleGenerationService(QObject):
    def __init__(
        self,
        parent: QWidget,
        player_window: PlayerWindow,
        store: MediaSettingsStore,
    ):
        super().__init__(parent)
        self._parent = parent
        self._player = player_window
        self._store = store
        self._subtitle_generation_dialog: SubtitleGenerationDialog | None = None
        self._subtitle_progress_dialog: SubtitleProgressDialog | None = None
        self._subtitle_thread: QThread | None = None
        self._subtitle_worker: SubtitleGenerationWorker | None = None
        self._cuda_runtime_thread: QThread | None = None
        self._cuda_runtime_worker: CudaRuntimeInstallWorker | None = None
        self._pending_subtitle_media_path: str | None = None
        self._pending_subtitle_options: SubtitleGenerationDialogResult | None = None

    def generate_subtitle(self) -> bool:
        current_media_path = self._player.playback.current_media_path()
        if not current_media_path:
            return False

        if self._player.playback.is_playing():
            self._player.pause()

        if self._subtitle_thread is not None or self._cuda_runtime_thread is not None:
            if self._subtitle_progress_dialog is not None:
                self._subtitle_progress_dialog.raise_()
                self._subtitle_progress_dialog.activateWindow()
            QMessageBox.information(
                self._parent,
                "Generate Subtitle",
                "Subtitle generation is already running.",
            )
            return False

        dialog = SubtitleGenerationDialog(
            theme_color=self._player.theme_color,
            metrics=get_metrics(self._parent),
            media_path=current_media_path,
            parent=self._parent,
        )
        dialog.set_audio_tracks(self._build_generation_audio_tracks())
        dialog.set_selected_audio_track(None)
        dialog.generateRequested.connect(self._start_subtitle_generation)
        dialog.canceled.connect(self._clear_generation_dialog_reference)
        dialog.destroyed.connect(self._clear_generation_dialog_reference)

        self._subtitle_generation_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return True

    def _build_generation_audio_tracks(self) -> list[tuple[int | None, str]]:
        tracks = self._player.get_audio_tracks()
        generated_tracks: list[tuple[int | None, str]] = [(None, "Current / default")]
        if not tracks:
            return generated_tracks
        generated_tracks.extend((index, title) for index, (_, title) in enumerate(tracks))
        return generated_tracks

    def _start_subtitle_generation(self, options: SubtitleGenerationDialogResult):
        if not self._validate_subtitle_options(options):
            return

        current_media_path = self._player.playback.current_media_path()
        if not current_media_path:
            return

        resolved_options = self._resolve_cuda_runtime_options(options)
        if resolved_options is None:
            return

        self._player.suspend_for_subtitle_generation()
        self._launch_subtitle_generation(current_media_path, resolved_options)

    def _launch_subtitle_generation(
        self,
        current_media_path: str,
        options: SubtitleGenerationDialogResult,
    ):
        generation_dialog = self._subtitle_generation_dialog
        if self._subtitle_generation_dialog is not None:
            self._subtitle_generation_dialog.hide()
            self._subtitle_generation_dialog = None

        progress_dialog = SubtitleProgressDialog(
            theme_color=self._player.theme_color,
            metrics=get_metrics(self._parent),
            parent=self._parent,
        )
        progress_dialog.set_status("Preparing subtitle generation...")
        progress_dialog.set_details(
            f"Device: {options.device or 'Auto'}\n"
            f"Model: {options.model_size}\n"
            f"Language: {options.audio_language or 'Auto detect'}\n"
            f"Output: {options.output_path}"
        )
        progress_dialog.cancelRequested.connect(self._cancel_subtitle_generation)
        progress_dialog.destroyed.connect(self._clear_progress_dialog_reference)
        progress_dialog.show()
        progress_dialog.raise_()
        progress_dialog.activateWindow()
        self._subtitle_progress_dialog = progress_dialog

        thread = QThread(self._parent)
        worker = SubtitleGenerationWorker(current_media_path, options)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.status_changed.connect(self._on_subtitle_status_changed)
        worker.progress_changed.connect(self._on_subtitle_progress_changed)
        worker.details_changed.connect(self._on_subtitle_details_changed)
        worker.finished.connect(self._on_subtitle_generation_finished)
        worker.failed.connect(self._on_subtitle_generation_failed)
        worker.canceled.connect(self._on_subtitle_generation_canceled)

        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.canceled.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._clear_subtitle_thread_references)
        thread.finished.connect(thread.deleteLater)

        self._subtitle_thread = thread
        self._subtitle_worker = worker
        thread.start()

        if generation_dialog is not None:
            generation_dialog.deleteLater()

    def _resolve_cuda_runtime_options(
        self,
        options: SubtitleGenerationDialogResult,
    ) -> SubtitleGenerationDialogResult | None:
        if options.device != "cuda":
            return options

        missing_packages = get_missing_windows_cuda_runtime_packages()
        if not missing_packages:
            return options

        choice = self._prompt_cuda_runtime_choice(missing_packages)
        if choice == "cancel":
            return None
        if choice == "cpu":
            return replace(options, device="cpu")

        current_media_path = self._player.playback.current_media_path()
        if not current_media_path:
            return None

        self._start_cuda_runtime_install(current_media_path, options, missing_packages)
        return None

    def _prompt_cuda_runtime_choice(self, missing_packages: list[str]) -> str:
        message_box = QMessageBox(self._parent)
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

    def _start_cuda_runtime_install(
        self,
        media_path: str,
        options: SubtitleGenerationDialogResult,
        missing_packages: list[str],
    ):
        self._player.suspend_for_subtitle_generation()
        self._pending_subtitle_media_path = media_path
        self._pending_subtitle_options = options

        generation_dialog = self._subtitle_generation_dialog
        if self._subtitle_generation_dialog is not None:
            self._subtitle_generation_dialog.hide()
            self._subtitle_generation_dialog = None

        progress_dialog = SubtitleProgressDialog(
            theme_color=self._player.theme_color,
            metrics=get_metrics(self._parent),
            parent=self._parent,
        )
        progress_dialog.set_status("Installing GPU runtime...")
        progress_dialog.set_indeterminate(True)
        progress_dialog.set_details(
            "Preparing NVIDIA CUDA runtime download...\n\n"
            "Packages:\n"
            + "\n".join(missing_packages)
        )
        progress_dialog.cancelRequested.connect(self._cancel_cuda_runtime_install)
        progress_dialog.destroyed.connect(self._clear_progress_dialog_reference)
        progress_dialog.show()
        progress_dialog.raise_()
        progress_dialog.activateWindow()
        self._subtitle_progress_dialog = progress_dialog

        thread = QThread(self._parent)
        worker = CudaRuntimeInstallWorker(missing_packages)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.status_changed.connect(self._on_subtitle_status_changed)
        worker.details_changed.connect(self._on_subtitle_details_changed)
        worker.finished.connect(self._on_cuda_runtime_install_finished)
        worker.failed.connect(self._on_cuda_runtime_install_failed)
        worker.canceled.connect(self._on_cuda_runtime_install_canceled)

        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.canceled.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._clear_cuda_runtime_thread_references)
        thread.finished.connect(thread.deleteLater)

        self._cuda_runtime_thread = thread
        self._cuda_runtime_worker = worker
        thread.start()

        if generation_dialog is not None:
            generation_dialog.deleteLater()

    def _validate_subtitle_options(self, options: SubtitleGenerationDialogResult) -> bool:
        output_path = options.output_path.strip()
        if not output_path:
            QMessageBox.warning(
                self._parent,
                "Generate Subtitle",
                "Choose an output path first.",
            )
            return False

        output_dir = os.path.dirname(output_path)
        if output_dir and not os.path.isdir(output_dir):
            QMessageBox.warning(
                self._parent,
                "Generate Subtitle",
                "The output folder does not exist.",
            )
            return False

        if os.path.exists(output_path):
            answer = QMessageBox.question(
                self._parent,
                "Overwrite Subtitle",
                f"Subtitle file already exists:\n{output_path}\n\nOverwrite it?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False

        return True

    @Slot()
    def _cancel_subtitle_generation(self):
        if self._subtitle_worker is not None:
            self._subtitle_worker.cancel()
        if self._subtitle_progress_dialog is not None:
            self._subtitle_progress_dialog.set_indeterminate(True)
            self._subtitle_progress_dialog.set_details("Cancelling subtitle generation...")

    @Slot()
    def _cancel_cuda_runtime_install(self):
        if self._cuda_runtime_worker is not None:
            self._cuda_runtime_worker.cancel()
        if self._subtitle_progress_dialog is not None:
            self._subtitle_progress_dialog.set_indeterminate(True)
            self._subtitle_progress_dialog.set_details(
                "Cancelling GPU runtime installation..."
            )

    @Slot(str)
    def _on_subtitle_status_changed(self, text: str):
        if self._subtitle_progress_dialog is not None:
            self._subtitle_progress_dialog.set_status(text)

    @Slot(int)
    def _on_subtitle_progress_changed(self, value: int):
        if self._subtitle_progress_dialog is not None:
            self._subtitle_progress_dialog.set_progress(value)

    @Slot(str)
    def _on_subtitle_details_changed(self, text: str):
        if self._subtitle_progress_dialog is not None:
            self._subtitle_progress_dialog.set_details(text)

    @Slot(str, bool)
    def _on_subtitle_generation_finished(self, output_path: str, auto_open: bool):
        self._player.resume_after_subtitle_generation()
        self._store.save_last_open_dir(output_path)
        if self._subtitle_progress_dialog is not None:
            self._subtitle_progress_dialog.close()
            self._subtitle_progress_dialog = None

        if auto_open:
            self._player.playback.open_subtitle_file(output_path)

        QMessageBox.information(
            self._parent,
            "Generate Subtitle",
            f"Subtitle file created:\n{output_path}",
        )

    @Slot(str)
    def _on_subtitle_generation_failed(self, error_text: str):
        self._player.resume_after_subtitle_generation()
        if self._subtitle_progress_dialog is not None:
            self._subtitle_progress_dialog.close()
            self._subtitle_progress_dialog = None
        QMessageBox.warning(self._parent, "Generate Subtitle", error_text)

    @Slot()
    def _on_subtitle_generation_canceled(self):
        self._player.resume_after_subtitle_generation()
        if self._subtitle_progress_dialog is not None:
            self._subtitle_progress_dialog.close()
            self._subtitle_progress_dialog = None
        QMessageBox.information(
            self._parent,
            "Generate Subtitle",
            "Subtitle generation was canceled.",
        )

    @Slot()
    def _on_cuda_runtime_install_finished(self):
        pending_media_path = self._pending_subtitle_media_path
        pending_options = self._pending_subtitle_options
        self._pending_subtitle_media_path = None
        self._pending_subtitle_options = None

        if self._subtitle_progress_dialog is not None:
            self._subtitle_progress_dialog.close()
            self._subtitle_progress_dialog = None

        if pending_media_path is None or pending_options is None:
            self._player.resume_after_subtitle_generation()
            return

        self._launch_subtitle_generation(pending_media_path, pending_options)

    @Slot(str)
    def _on_cuda_runtime_install_failed(self, error_text: str):
        self._pending_subtitle_media_path = None
        self._pending_subtitle_options = None
        self._player.resume_after_subtitle_generation()
        if self._subtitle_progress_dialog is not None:
            self._subtitle_progress_dialog.close()
            self._subtitle_progress_dialog = None
        QMessageBox.warning(self._parent, "CUDA Runtime", error_text)

    @Slot()
    def _on_cuda_runtime_install_canceled(self):
        self._pending_subtitle_media_path = None
        self._pending_subtitle_options = None
        self._player.resume_after_subtitle_generation()
        if self._subtitle_progress_dialog is not None:
            self._subtitle_progress_dialog.close()
            self._subtitle_progress_dialog = None
        QMessageBox.information(
            self._parent,
            "CUDA Runtime",
            "GPU runtime installation was canceled.",
        )

    @Slot()
    def _clear_subtitle_thread_references(self, *_args):
        self._subtitle_thread = None
        self._subtitle_worker = None

    @Slot()
    def _clear_cuda_runtime_thread_references(self, *_args):
        self._cuda_runtime_thread = None
        self._cuda_runtime_worker = None

    @Slot()
    def _clear_generation_dialog_reference(self, *_args):
        self._subtitle_generation_dialog = None

    @Slot()
    def _clear_progress_dialog_reference(self, *_args):
        self._subtitle_progress_dialog = None
