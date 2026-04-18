import logging

from PySide6.QtCore import QObject, Signal, Slot

from services.runtime.CudaRuntimeInstaller import resolve_cuda_runtime_install_target
from services.runtime.RuntimeExecution import build_runtime_installer_launch
from services.runtime.JsonSubprocessWorker import JsonSubprocessWorkerBase
from services.runtime.RuntimeInstallerProtocol import CudaRuntimeInstallRequest
from services.runtime.RuntimeInstallerProtocol import (
    EVENT_CANCELED,
    EVENT_FAILED,
    EVENT_FINISHED,
    EVENT_STATUS,
    INSTALLER_CUDA_RUNTIME,
)
from services.runtime.SubprocessWorkerSupport import (
    BoundedLineBuffer,
    build_exception_diagnostics,
    build_process_diagnostics,
)


logger = logging.getLogger(__name__)


class CudaRuntimeInstallWorker(QObject, JsonSubprocessWorkerBase):
    status_changed = Signal(str)
    details_changed = Signal(str)
    finished = Signal()
    failed = Signal(str)
    canceled = Signal()

    _GRACEFUL_CANCEL_TIMEOUT_SECONDS = 1.5
    _MAX_DIAGNOSTIC_LINES = 200
    _MAX_USER_ERROR_LINES = 12

    def __init__(self, packages: list[str]):
        super().__init__()
        self._request = CudaRuntimeInstallRequest(
            packages=tuple(str(package).strip() for package in packages if str(package).strip()),
            install_target=str(resolve_cuda_runtime_install_target()),
        )
        self._init_json_subprocess_worker()
        self._stderr_buffer = BoundedLineBuffer(max_lines=self._MAX_DIAGNOSTIC_LINES)
        self._stdout_buffer = BoundedLineBuffer(max_lines=self._MAX_DIAGNOSTIC_LINES)

    @Slot()
    def run(self):
        launch_spec = build_runtime_installer_launch(INSTALLER_CUDA_RUNTIME)
        logger.info(
            "CUDA runtime installer worker started | packages=%s | target=%s | execution_mode=%s",
            ", ".join(self._request.packages),
            self._request.install_target,
            launch_spec.execution_mode,
        )
        self.status_changed.emit("Preparing GPU runtime installer...")
        self.details_changed.emit(
            "Launching isolated installer subsystem...\n"
            "The installer resolves its source, target, and bootstrap runtime independently."
        )

        if self._is_cancel_requested():
            logger.info("CUDA runtime installer worker canceled before launch")
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
            logger.exception(
                "CUDA runtime installer worker crashed while invoking subsystem | diagnostics=%s",
                diagnostics or "<none>",
            )
            if self._is_cancel_requested():
                self._emit_canceled()
            else:
                self._emit_failed("GPU runtime installation failed to start.", diagnostics)
            return

        if self._terminal_event_already_emitted():
            logger.info("CUDA runtime installer worker finished after terminal event")
            return

        if self._is_cancel_requested():
            logger.info(
                "CUDA runtime installer worker canceled after subsystem finished | returncode=%s",
                result.return_code,
            )
            self._emit_canceled()
            return

        if result.process is None or result.process.returncode is None:
            logger.error("CUDA runtime installer worker finished without a valid process result")
            self._emit_failed("GPU runtime installation did not finish correctly.", "")
            return

        diagnostics = self._build_process_diagnostics(result.process.returncode)
        error_text = self._build_user_error_text() or "Unknown installer error."
        logger.error(
            "CUDA runtime installer worker failed without terminal event | returncode=%s | details=%s",
            result.process.returncode,
            diagnostics or error_text,
        )
        self._emit_failed("Failed to install GPU runtime:", error_text)

    def cancel(self):
        if not self._request_graceful_subprocess_stop(self._on_cancel_requested):
            logger.info("Repeated cancel request ignored for CUDA runtime installer worker")
            return

    def _on_cancel_requested(self):
        logger.info("CUDA runtime installer worker cancel requested")
        self.status_changed.emit("Cancelling GPU runtime installation...")

    def force_stop(self):
        self._request_force_subprocess_stop(
            self._on_force_stop_requested,
            self._on_repeated_force_stop_requested,
            self._on_force_stop_kill_failed,
        )

    def _on_force_stop_requested(self):
        logger.warning("Force-stop requested for CUDA runtime installer worker")

    def _on_repeated_force_stop_requested(self):
        logger.info("Repeated force-stop request ignored for CUDA runtime installer worker")

    def _on_force_stop_kill_failed(self, process):
        logger.exception(
            "Failed to hard-stop CUDA runtime installer process immediately | pid=%s",
            process.pid,
        )

    def _handle_invalid_json_stdout(self, line: str):
        logger.warning("CUDA runtime installer emitted invalid JSON event | line=%s", line)
        self._stdout_buffer.append(f"Invalid stdout event: {line}")

    def _handle_json_event(self, event_type: str, event: dict, line: str):
        if event_type == EVENT_STATUS:
            self.status_changed.emit(str(event.get("status") or "Installing GPU runtime..."))
            self.details_changed.emit(str(event.get("details") or ""))
            return
        if event_type == EVENT_FINISHED:
            self._emit_finished()
            return
        if event_type == EVENT_FAILED:
            self._emit_failed(
                str(event.get("user_message") or "Failed to install GPU runtime."),
                str(event.get("diagnostics") or ""),
            )
            return
        if event_type == EVENT_CANCELED:
            self._emit_canceled()
            return

        logger.warning("CUDA runtime installer emitted unknown event type | event=%s", event_type or "<missing>")
        self._stdout_buffer.append(f"Unknown stdout event: {line}")

    def _on_json_subprocess_deferred_cancel(self, process):
        logger.info(
            "CUDA runtime installer worker received deferred cancel immediately after launch | pid=%s",
            process.pid,
        )

    def _emit_finished(self):
        if not self._mark_terminal_event_emitted():
            return
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
        if not self._mark_terminal_event_emitted():
            return
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
        process = self._process or getattr(self, "_last_json_subprocess_process", None)
        return_code = process.returncode if process is not None else None
        return build_exception_diagnostics(
            exc,
            [("process", self._build_process_diagnostics(return_code))],
        )

    def _build_user_error_text(self) -> str:
        stderr_text = self._stderr_buffer.consume_text()
        stdout_text = self._stdout_buffer.consume_text()
        source_text = stderr_text or stdout_text
        if not source_text:
            return ""
        lines = [line for line in source_text.splitlines() if line.strip()]
        return "\n".join(lines[-self._MAX_USER_ERROR_LINES :]).strip()

    def _subprocess_log_name(self) -> str:
        return "CUDA runtime installer process"

    def _json_subprocess_display_name(self) -> str:
        return "CUDA runtime installer"
