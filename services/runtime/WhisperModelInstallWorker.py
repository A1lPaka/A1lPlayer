import logging

from PySide6.QtCore import QObject, Signal, Slot

from services.runtime.JsonSubprocessWorker import JsonSubprocessWorkerBase
from services.runtime.RuntimeExecution import build_runtime_installer_launch
from services.runtime.RuntimeInstallerProtocol import (
    EVENT_CANCELED,
    EVENT_FAILED,
    EVENT_FINISHED,
    EVENT_STATUS,
    INSTALLER_WHISPER_MODEL,
    WhisperModelInstallRequest,
)
from services.runtime.SubprocessWorkerSupport import (
    BoundedLineBuffer,
    build_exception_diagnostics,
    build_process_diagnostics,
)
from services.runtime.WhisperModelInstaller import resolve_whisper_model_install_target


logger = logging.getLogger(__name__)


class WhisperModelInstallWorker(QObject, JsonSubprocessWorkerBase):
    status_changed = Signal(str)
    details_changed = Signal(str)
    finished = Signal()
    failed = Signal(str)
    canceled = Signal()

    _GRACEFUL_CANCEL_TIMEOUT_SECONDS = 1.5
    _MAX_DIAGNOSTIC_LINES = 200
    _MAX_USER_ERROR_LINES = 12

    def __init__(self, model_size: str):
        super().__init__()
        self._request = WhisperModelInstallRequest(
            model_size=str(model_size).strip(),
            install_target=str(resolve_whisper_model_install_target(model_size)),
        )
        self._init_json_subprocess_worker()
        self._stderr_buffer = BoundedLineBuffer(max_lines=self._MAX_DIAGNOSTIC_LINES)
        self._stdout_buffer = BoundedLineBuffer(max_lines=self._MAX_DIAGNOSTIC_LINES)

    @Slot()
    def run(self):
        launch_spec = build_runtime_installer_launch(INSTALLER_WHISPER_MODEL)
        logger.info(
            "Whisper model installer worker started | model=%s | target=%s | execution_mode=%s",
            self._request.model_size,
            self._request.install_target,
            launch_spec.execution_mode,
        )
        self.status_changed.emit("Preparing Whisper model download...")
        self.details_changed.emit(
            "Launching isolated installer subsystem...\n"
            "The model will be stored in the configured runtime models directory."
        )

        if self._is_cancel_requested():
            self._emit_canceled()
            return

        try:
            result = self._run_json_subprocess(
                launch_spec=launch_spec,
                request_json=self._request.to_json(),
                stderr_buffer=self._stderr_buffer,
                read_stdout_in_thread=True,
            )
        except Exception as exc:
            diagnostics = self._build_exception_diagnostics(exc)
            logger.exception("Whisper model installer worker crashed | diagnostics=%s", diagnostics or "<none>")
            if self._is_cancel_requested():
                self._emit_canceled()
            else:
                self._emit_failed("Whisper model installation failed to start.", diagnostics)
            return

        if self._terminal_event_already_emitted():
            return

        if self._is_cancel_requested():
            self._emit_canceled()
            return

        diagnostics = self._build_process_diagnostics(result.return_code)
        self._emit_failed("Failed to install Whisper model:", diagnostics or self._build_user_error_text())

    def cancel(self):
        if not self._request_graceful_subprocess_stop(self._on_cancel_requested):
            return

    def _on_cancel_requested(self):
        logger.info("Whisper model installer worker cancel requested | model=%s", self._request.model_size)

    def force_stop(self):
        self._request_force_subprocess_stop(
            self._on_force_stop_requested,
            self._on_repeated_force_stop_requested,
            self._on_force_stop_kill_failed,
        )

    def _on_force_stop_requested(self):
        logger.warning("Force-stop requested for Whisper model installer worker")

    def _on_repeated_force_stop_requested(self):
        logger.info("Repeated force-stop request ignored for Whisper model installer worker")

    def _on_force_stop_kill_failed(self, process):
        logger.exception("Failed to hard-stop Whisper model installer process | pid=%s", process.pid)

    def _handle_invalid_json_stdout(self, line: str):
        logger.warning("Whisper model installer emitted invalid JSON event | line=%s", line)
        self._stdout_buffer.append(f"Invalid stdout event: {line}")

    def _handle_json_event(self, event_type: str, event: dict, line: str):
        if event_type == EVENT_STATUS:
            self.status_changed.emit(str(event.get("status") or "Downloading Whisper model..."))
            self.details_changed.emit(str(event.get("details") or ""))
            return
        if event_type == EVENT_FINISHED:
            self._emit_finished()
            return
        if event_type == EVENT_FAILED:
            self._emit_failed(
                str(event.get("user_message") or "Failed to install Whisper model."),
                str(event.get("diagnostics") or ""),
            )
            return
        if event_type == EVENT_CANCELED:
            self._emit_canceled()
            return

        logger.warning("Whisper model installer emitted unknown event type | event=%s", event_type or "<missing>")
        self._stdout_buffer.append(f"Unknown stdout event: {line}")

    def _on_json_subprocess_deferred_cancel(self, process):
        logger.info("Whisper model installer received deferred cancel | pid=%s", process.pid)

    def _emit_finished(self):
        if self._mark_terminal_event_emitted():
            self.finished.emit()

    def _emit_failed(self, user_message: str, diagnostics: str):
        if not self._mark_terminal_event_emitted():
            return
        error_text = str(user_message)
        details = str(diagnostics).strip()
        if details:
            error_text = f"{error_text}\n{details}"
        self.failed.emit(error_text)

    def _emit_canceled(self):
        if self._mark_terminal_event_emitted():
            self.canceled.emit()

    def _build_process_diagnostics(self, return_code: int | None) -> str:
        return build_process_diagnostics(
            return_code,
            [
                ("stderr", self._stderr_buffer.consume_text()),
                ("stdout", self._stdout_buffer.consume_text()),
            ],
        )

    def _build_exception_diagnostics(self, exc: BaseException) -> str:
        return build_exception_diagnostics(exc, [("process", self._build_process_diagnostics(None))])

    def _build_user_error_text(self) -> str:
        source_text = self._stderr_buffer.consume_text() or self._stdout_buffer.consume_text()
        lines = [line for line in source_text.splitlines() if line.strip()]
        return "\n".join(lines[-self._MAX_USER_ERROR_LINES :]).strip()

    def _subprocess_log_name(self) -> str:
        return "Whisper model installer process"

    def _json_subprocess_display_name(self) -> str:
        return "Whisper model installer"
