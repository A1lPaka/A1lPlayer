from __future__ import annotations

import json
import logging
import signal
import sys
import threading

from services.runtime.CudaRuntimeInstaller import (
    CudaRuntimeInstallCanceledError,
    build_cuda_runtime_failure_event,
    ensure_cuda_runtime_installed,
)
from services.runtime.RuntimeExecution import get_runtime_mode_label
from services.runtime.RuntimeInstallerProtocol import (
    CudaRuntimeInstallRequest,
    INSTALLER_CUDA_RUNTIME,
    build_canceled_event,
)
from utils.LoggingSetup import configure_logging


logger = logging.getLogger(__name__)


def _configure_stdio_utf8():
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def try_run_runtime_installer(argv: list[str] | None = None) -> int | None:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2 or args[0] != "--installer":
        return None

    installer_name = str(args[1]).strip().lower()
    _configure_stdio_utf8()
    configure_logging()
    logger.info(
        "Runtime installer mode activated | installer=%s | runtime_mode=%s | argv=%s | stdin_encoding=%s | stdout_encoding=%s",
        installer_name,
        get_runtime_mode_label(),
        args,
        getattr(sys.stdin, "encoding", None),
        getattr(sys.stdout, "encoding", None),
    )

    if installer_name == INSTALLER_CUDA_RUNTIME:
        return run_cuda_runtime_installer()

    logger.error("Unknown runtime installer requested | installer=%s", installer_name)
    sys.stderr.write(f"Unknown installer: {installer_name}\n")
    sys.stderr.flush()
    return 64


def run_cuda_runtime_installer() -> int:
    cancel_event = threading.Event()
    _install_signal_handlers(cancel_event)

    request: CudaRuntimeInstallRequest | None = None
    try:
        request = CudaRuntimeInstallRequest.from_json(_read_stdin_payload())
        ensure_cuda_runtime_installed(
            request=request,
            emit_event=_emit_event,
            cancel_event=cancel_event,
        )
        return 0
    except (CudaRuntimeInstallCanceledError, KeyboardInterrupt):
        logger.info("CUDA installer subsystem canceled")
        _emit_event(build_canceled_event())
        return 2
    except Exception as exc:
        logger.exception(
            "CUDA installer subsystem failed | target=%s | packages=%s",
            request.install_target if request is not None else "<unknown>",
            ", ".join(request.packages) if request is not None else "<unknown>",
        )
        _emit_event(build_cuda_runtime_failure_event(exc))
        return 1


def _emit_event(event: dict):
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _read_stdin_payload() -> str:
    payload = sys.stdin.read().strip()
    if not payload:
        raise RuntimeError("Installer request payload is missing.")
    return payload


def _install_signal_handlers(cancel_event: threading.Event):
    def _handle_signal(signum, _frame):
        logger.warning("CUDA installer subsystem received termination signal | signal=%s", signum)
        cancel_event.set()
        raise CudaRuntimeInstallCanceledError(f"Interrupted by signal {signum}")

    for signal_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signal_value = getattr(signal, signal_name, None)
        if signal_value is None:
            continue
        try:
            signal.signal(signal_value, _handle_signal)
        except (OSError, RuntimeError, ValueError):
            continue
