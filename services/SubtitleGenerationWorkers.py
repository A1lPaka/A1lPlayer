import subprocess
import sys
import threading

from PySide6.QtCore import QObject, Signal, Slot

from services.SubtitleMaker import (
    SubtitleGenerationCanceledError,
    SubtitleGenerationEmptyResultError,
    SubtitleMaker,
    get_missing_windows_cuda_runtime_packages,
)
from ui.SubtitleGenerationDialog import SubtitleGenerationDialogResult


class SubtitleGenerationWorker(QObject):
    progress_changed = Signal(int)
    status_changed = Signal(str)
    details_changed = Signal(str)
    finished = Signal(str, bool)
    failed = Signal(str)
    canceled = Signal()

    def __init__(self, media_path: str, options: SubtitleGenerationDialogResult):
        super().__init__()
        self._media_path = media_path
        self._options = options
        self._cancel_event = threading.Event()
        self._maker: SubtitleMaker | None = None

    @Slot()
    def run(self):
        try:
            self.status_changed.emit("Preparing...")
            self.progress_changed.emit(0)
            self.details_changed.emit(self._build_details())

            if self._cancel_event.is_set():
                self.canceled.emit()
                return

            maker = SubtitleMaker(
                model_size=self._options.model_size,
                device=self._options.device,
            )
            self._maker = maker

            segments = maker.transcribe_file(
                self._media_path,
                audio_track=self._options.audio_track_id,
                language=self._options.audio_language,
                progress_callback=self._on_progress,
                cancel_event=self._cancel_event,
            )

            if self._cancel_event.is_set():
                self.canceled.emit()
                return

            self.status_changed.emit("Saving subtitles...")
            self.progress_changed.emit(97)
            self.details_changed.emit(
                self._build_details(stage="Saving", actual_device=maker.device)
            )
            maker.save_subtitles(
                segments,
                self._options.output_path,
                self._options.output_format,
            )
            self.progress_changed.emit(100)
            self.finished.emit(
                self._options.output_path,
                self._options.auto_open_after_generation,
            )
        except SubtitleGenerationCanceledError:
            self.canceled.emit()
        except SubtitleGenerationEmptyResultError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self._maker = None

    def cancel(self):
        self._cancel_event.set()
        if self._maker is not None:
            self._maker.cancel()
        self.status_changed.emit("Cancelling...")

    def _on_progress(self, status: str, progress: int, details: str):
        if self._cancel_event.is_set():
            raise SubtitleGenerationCanceledError()
        self.status_changed.emit(str(status))
        self.progress_changed.emit(int(progress))
        if details:
            self.details_changed.emit(str(details))

    def _build_details(
        self,
        stage: str | None = None,
        actual_device: str | None = None,
    ) -> str:
        language_label = self._options.audio_language or "Auto detect"
        if self._options.audio_track_id is None:
            track_label = "Current / default"
        else:
            track_label = f"Track {int(self._options.audio_track_id) + 1}"
        device_label = actual_device or self._options.device or "Auto"

        lines = []
        if stage is not None:
            lines.append(f"Stage: {stage}")
        lines.extend(
            [
                f"Media: {self._media_path}",
                f"Audio: {track_label}",
                f"Language: {language_label}",
                f"Device: {device_label}",
                f"Model: {self._options.model_size}",
                f"Output: {self._options.output_path}",
            ]
        )
        return "\n".join(lines)


class CudaRuntimeInstallWorker(QObject):
    status_changed = Signal(str)
    details_changed = Signal(str)
    finished = Signal()
    failed = Signal(str)
    canceled = Signal()

    def __init__(self, packages: list[str]):
        super().__init__()
        self._packages = list(packages)
        self._cancel_event = threading.Event()
        self._process: object | None = None

    @Slot()
    def run(self):
        self.status_changed.emit("Installing GPU runtime...")
        self.details_changed.emit(
            "Downloading required NVIDIA CUDA libraries...\n"
            "This is needed once for CUDA subtitle generation.\n\n"
            "Packages:\n"
            + "\n".join(self._packages)
        )

        if self._cancel_event.is_set():
            self.canceled.emit()
            return

        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "pip", "install", "--upgrade", *self._packages],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
            )
            self._process = process
            stdout_data, stderr_data = process.communicate()
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        finally:
            process = self._process
            self._process = None

        if self._cancel_event.is_set():
            self.canceled.emit()
            return

        if process is None or process.returncode is None:
            self.failed.emit("GPU runtime installation did not finish correctly.")
            return

        if process.returncode != 0:
            error_text = (
                self._decode_process_output(stderr_data)
                or self._decode_process_output(stdout_data)
                or "Unknown pip error."
            )
            self.failed.emit(f"Failed to install GPU runtime:\n{error_text}")
            return

        missing_packages = get_missing_windows_cuda_runtime_packages()
        if missing_packages:
            self.failed.emit(
                "GPU runtime installation finished, but required CUDA libraries are still missing:\n"
                + "\n".join(missing_packages)
            )
            return

        self.finished.emit()

    def cancel(self):
        self._cancel_event.set()
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        self.status_changed.emit("Cancelling GPU runtime installation...")

    def _decode_process_output(self, payload: bytes | None) -> str:
        if not payload:
            return ""
        return payload.decode("utf-8", errors="replace").strip()
