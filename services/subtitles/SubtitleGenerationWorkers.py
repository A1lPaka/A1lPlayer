import logging
import os
import signal
import subprocess
import time

from PySide6.QtCore import QObject, Signal, Slot

from models import SubtitleGenerationDialogResult
from services.runtime.RuntimeExecution import build_runtime_helper_launch
from services.runtime.RuntimeHelperProtocol import (
    EVENT_CANCELED,
    EVENT_FAILED,
    EVENT_FINISHED,
    EVENT_PROGRESS,
    HELPER_SUBTITLE_GENERATION,
    SubtitleGenerationRequest,
)
from services.runtime.JsonSubprocessWorker import JsonSubprocessWorkerBase
from services.subtitles.AudioStreamProbe import probe_audio_streams
from services.subtitles.SubtitleTiming import elapsed_ms_since, log_timing
from services.runtime.SubprocessWorkerSupport import (
    BoundedLineBuffer,
    build_exception_diagnostics,
    build_process_diagnostics,
)


logger = logging.getLogger(__name__)


class AudioStreamProbeWorker(QObject):
    finished = Signal(int, str, object)
    failed = Signal(int, str, str)

    def __init__(self, probe_request_id: int, media_path: str):
        super().__init__()
        self._probe_request_id = int(probe_request_id)
        self._media_path = str(media_path)

    @Slot()
    def run(self):
        try:
            logger.info(
                "Starting background audio stream probe | probe_request_id=%s | media=%s",
                self._probe_request_id,
                self._media_path,
            )
            audio_streams = probe_audio_streams(self._media_path)
        except Exception as exc:
            logger.warning(
                "Background audio stream probe failed | probe_request_id=%s | media=%s | reason=%s",
                self._probe_request_id,
                self._media_path,
                exc,
            )
            self.failed.emit(self._probe_request_id, self._media_path, str(exc))
            return

        self.finished.emit(self._probe_request_id, self._media_path, list(audio_streams))


class SubtitleGenerationWorker(QObject, JsonSubprocessWorkerBase):
    progress_changed = Signal(int)
    status_changed = Signal(str)
    details_changed = Signal(str)
    finished = Signal(str, bool, bool)
    failed = Signal(str, str)
    canceled = Signal()

    _GRACEFUL_CANCEL_TIMEOUT_SECONDS = 1.5

    def __init__(self, run_id: int, media_path: str, options: SubtitleGenerationDialogResult):
        super().__init__()
        self._run_id = run_id
        self._request = SubtitleGenerationRequest(
            media_path=media_path,
            audio_stream_index=options.audio_stream_index,
            audio_language=options.audio_language,
            device=options.device,
            model_size=options.model_size,
            output_format=options.output_format,
            output_path=options.output_path,
            auto_open_after_generation=options.auto_open_after_generation,
        )
        self._init_json_subprocess_worker()
        self._stderr_buffer = BoundedLineBuffer(max_lines=200)
        self._worker_started_at: float | None = None
        self._spawn_started_at: float | None = None
        self._first_event_logged = False

    @Slot()
    def run(self):
        self._worker_started_at = time.perf_counter()
        launch_spec = build_runtime_helper_launch(HELPER_SUBTITLE_GENERATION)

        self.status_changed.emit("Preparing...")
        self.progress_changed.emit(0)
        self.details_changed.emit(self._build_initial_details())

        if self._is_cancel_requested():
            logger.info("Subtitle generation subprocess worker canceled before launch | media=%s", self._request.media_path)
            self._emit_canceled()
            return

        try:
            logger.info(
                "Launching subtitle generation helper subprocess | media=%s | output=%s | model=%s | requested_device=%s | audio_stream_index=%s | language=%s | execution_mode=%s",
                self._request.media_path,
                self._request.output_path,
                self._request.model_size,
                self._request.device or "auto",
                self._request.audio_stream_index,
                self._request.audio_language or "auto",
                launch_spec.execution_mode,
            )
            result = self._run_json_subprocess(
                launch_spec=launch_spec,
                request_json=self._request.to_json(),
                stderr_buffer=self._stderr_buffer,
            )
            return_code = result.return_code

            if self._terminal_event_already_emitted():
                logger.info(
                    "Subtitle generation subprocess finished after terminal event | media=%s | returncode=%s",
                    self._request.media_path,
                    return_code,
                )
                return

            diagnostics = self._build_process_diagnostics(return_code)
            if self._is_cancel_requested():
                logger.info(
                    "Subtitle generation subprocess exited during cancellation | media=%s | returncode=%s",
                    self._request.media_path,
                    return_code,
                )
                self._emit_canceled()
                return

            logger.error(
                "Subtitle generation subprocess exited without terminal event | media=%s | returncode=%s | diagnostics=%s",
                self._request.media_path,
                return_code,
                diagnostics or "<none>",
            )
            self._emit_failed(
                "Subtitle generation stopped unexpectedly.",
                diagnostics,
            )
        except Exception as exc:
            diagnostics = self._build_exception_diagnostics(exc)
            logger.exception(
                "Subtitle generation helper worker crashed | media=%s | output=%s",
                self._request.media_path,
                self._request.output_path,
            )
            self._emit_failed("Subtitle generation failed to start.", diagnostics)

    def cancel(self):
        if not self._request_graceful_subprocess_stop(self._on_cancel_requested):
            logger.info("Repeated cancel request ignored for subtitle generation subprocess | media=%s", self._request.media_path)
            return

    def _on_cancel_requested(self):
        logger.info("Cancel requested for subtitle generation subprocess | media=%s", self._request.media_path)
        self.status_changed.emit("Cancellation requested...")
        self.details_changed.emit(self._build_cancel_details())

    def force_stop(self):
        self._request_force_subprocess_stop(
            self._on_force_stop_requested,
            self._on_repeated_force_stop_requested,
            self._on_force_stop_kill_failed,
        )

    def _on_force_stop_requested(self):
        logger.warning(
            "Force-stop requested for subtitle generation subprocess | media=%s",
            self._request.media_path,
        )

    def _on_repeated_force_stop_requested(self):
        logger.info(
            "Repeated force-stop request ignored for subtitle generation subprocess | media=%s",
            self._request.media_path,
        )

    def _on_force_stop_kill_failed(self, process: subprocess.Popen[str]):
        logger.exception(
            "Failed to hard-stop subtitle generation subprocess immediately | media=%s | pid=%s",
            self._request.media_path,
            process.pid,
        )

    def _handle_invalid_json_stdout(self, line: str):
        logger.warning(
            "Subtitle generation subprocess emitted invalid JSON event | media=%s | line=%s",
            self._request.media_path,
            line,
        )
        self._stderr_buffer.append(f"Invalid stdout event: {line}")

    def _handle_json_event(self, event_type: str, event: dict, line: str):
        self._log_first_helper_event(event_type)
        if event_type == EVENT_PROGRESS:
            self.status_changed.emit(str(event.get("status") or "Working..."))
            self.progress_changed.emit(int(event.get("progress") or 0))
            self.details_changed.emit(str(event.get("details") or ""))
            return
        if event_type == EVENT_FINISHED:
            self._emit_finished(
                str(event.get("output_path") or self._request.output_path),
                bool(event.get("auto_open")),
                bool(event.get("used_fallback_output_path")),
            )
            return
        if event_type == EVENT_FAILED:
            self._emit_failed(
                str(event.get("user_message") or "Subtitle generation failed."),
                str(event.get("diagnostics") or ""),
            )
            return
        if event_type == EVENT_CANCELED:
            self._emit_canceled()
            return

        logger.warning(
            "Subtitle generation subprocess emitted unknown event type | media=%s | event=%s",
            self._request.media_path,
            event_type or "<missing>",
        )
        self._stderr_buffer.append(f"Unknown stdout event: {line}")

    def _after_json_subprocess_spawned(self, process: subprocess.Popen[str], launch_spec):
        log_timing(
            logger,
            "Subtitle timing",
            "helper_subprocess_spawn",
            elapsed_ms_since(self._spawn_started_at or time.perf_counter()),
            run_id=self._run_id,
            media=self._request.media_path,
            output=self._request.output_path,
            pid=process.pid,
            execution_mode=launch_spec.execution_mode,
        )

    def _spawn_json_subprocess(self, launch_spec):
        self._spawn_started_at = time.perf_counter()
        return super()._spawn_json_subprocess(launch_spec)

    def _on_json_subprocess_deferred_cancel(self, process: subprocess.Popen[str]):
        logger.info(
            "Subtitle generation subprocess received deferred cancel immediately after launch | media=%s | pid=%s",
            self._request.media_path,
            process.pid,
        )

    def _log_first_helper_event(self, event_type: str):
        if self._first_event_logged or self._worker_started_at is None:
            return

        self._first_event_logged = True
        log_timing(
            logger,
            "Subtitle timing",
            "first_helper_event",
            elapsed_ms_since(self._worker_started_at),
            run_id=self._run_id,
            media=self._request.media_path,
            output=self._request.output_path,
            event=event_type or "<missing>",
        )

    def _emit_failed(self, user_message: str, diagnostics: str):
        if not self._mark_terminal_event_emitted():
            return
        self.failed.emit(str(user_message), str(diagnostics))

    def _emit_canceled(self):
        if not self._mark_terminal_event_emitted():
            return
        self.canceled.emit()

    def _emit_finished(self, output_path: str, auto_open: bool, used_fallback_output_path: bool):
        if not self._mark_terminal_event_emitted():
            return
        self.finished.emit(str(output_path), bool(auto_open), bool(used_fallback_output_path))

    def _build_initial_details(self) -> str:
        language_label = self._request.audio_language or "Auto detect"
        if self._request.audio_stream_index is None:
            track_label = "Current / default"
        else:
            track_label = f"Stream #{int(self._request.audio_stream_index)}"
        device_label = self._request.device or "Auto"
        return "\n".join(
            [
                f"Media: {self._request.media_path}",
                f"Audio: {track_label}",
                f"Language: {language_label}",
                f"Device: {device_label}",
                f"Model: {self._request.model_size}",
                f"Output: {self._request.output_path}",
            ]
        )

    def _build_cancel_details(self) -> str:
        return "\n".join(
            [
                "Stopping subtitle generation subprocess.",
                "The current transcription job will be terminated outside the GUI process.",
                "",
                self._build_initial_details(),
            ]
        )

    def _build_process_diagnostics(self, return_code: int | None) -> str:
        return build_process_diagnostics(
            return_code,
            [(None, self._stderr_buffer.consume_text())],
        )

    def _build_exception_diagnostics(self, exc: BaseException) -> str:
        return build_exception_diagnostics(
            exc,
            [(None, self._stderr_buffer.consume_text())],
        )

    def _request_graceful_stop(self, process: subprocess.Popen[str]):
        if process.poll() is not None:
            return

        if os.name == "nt":
            ctrl_break = getattr(signal, "CTRL_BREAK_EVENT", None)
            if ctrl_break is not None:
                process.send_signal(ctrl_break)
                return
            process.terminate()
            return

        os.killpg(os.getpgid(process.pid), signal.SIGTERM)

    def _subprocess_log_name(self) -> str:
        return "subtitle generation subprocess"

    def _json_subprocess_display_name(self) -> str:
        return "Subtitle generation process"
