from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import site
import subprocess
import sys
import threading
import time

from services.runtime.RuntimeExecution import get_runtime_mode_label, is_frozen_runtime
from services.runtime.RuntimeInstallerProtocol import (
    CudaRuntimeInstallRequest,
    build_failed_event,
    build_finished_event,
    build_status_event,
)
from services.runtime.SubprocessWorkerSupport import BoundedLineBuffer
from services.subtitles.SubtitleMaker import get_missing_windows_cuda_runtime_packages


logger = logging.getLogger(__name__)

_CUDA_WHEELHOUSE_ENV = "A1LPLAYER_CUDA_WHEELHOUSE"
_CUDA_INDEX_URL_ENV = "A1LPLAYER_CUDA_INDEX_URL"
_CUDA_EXTRA_INDEX_URL_ENV = "A1LPLAYER_CUDA_EXTRA_INDEX_URL"
_INSTALLER_PYTHON_ENV = "A1LPLAYER_INSTALLER_PYTHON"
_DEFAULT_CUDA_INDEX_URL = "https://pypi.org/simple"
_DEFAULT_WHEELHOUSE_RELATIVE_PATH = Path("runtime") / "cuda-wheelhouse"

# Technical debt note:
# The CUDA runtime installer is intentionally "best effort" until the project
# reaches the packaging/distribution stage. At that point this flow should be
# revisited and replaced with a release-grade solution: bundled Python runtime,
# offline wheelhouse, or a dedicated external installer/bootstrapper.
# Until then, source-mode development and frozen-runtime probing are supported,
# but deployment guarantees are intentionally limited.


class CudaRuntimeInstallCanceledError(RuntimeError):
    pass


@dataclass(frozen=True)
class CudaRuntimeInstallSource:
    mode: str
    pip_args: tuple[str, ...]
    location: str


class _InstallerStatusReporter:
    def __init__(
        self,
        request: CudaRuntimeInstallRequest,
        source: CudaRuntimeInstallSource,
        diagnostic_buffer: BoundedLineBuffer,
        emit_event,
    ):
        self._request = request
        self._source = source
        self._diagnostic_buffer = diagnostic_buffer
        self._emit_event = emit_event
        self._lock = threading.Lock()
        self._last_emit_monotonic = 0.0

    def emit(self, status: str, include_tail: bool = True, force: bool = False):
        now = time.monotonic()
        with self._lock:
            if not force and now - self._last_emit_monotonic < 0.2:
                return
            self._last_emit_monotonic = now

        details_parts = [
            f"Runtime mode: {get_runtime_mode_label()}",
            f"Install source: {self._source.mode}",
            f"Source location: {self._source.location}",
            f"Install target: {self._request.install_target}",
            "Packages:",
            *(self._request.packages or ("<none>",)),
        ]
        if include_tail:
            tail_text = self._diagnostic_buffer.tail(12)
            if tail_text:
                details_parts.extend(["", "Installer output:", tail_text])
        self._emit_event(build_status_event(status, "\n".join(details_parts)))

def resolve_cuda_runtime_install_target() -> Path:
    target = Path(site.getusersitepackages())
    target.mkdir(parents=True, exist_ok=True)
    return target


def resolve_runtime_app_root() -> Path:
    if is_frozen_runtime():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resolve_cuda_runtime_install_source() -> CudaRuntimeInstallSource:
    app_root = resolve_runtime_app_root()
    configured_wheelhouse = _read_optional_env_path(_CUDA_WHEELHOUSE_ENV)
    wheelhouse_path = configured_wheelhouse or (app_root / _DEFAULT_WHEELHOUSE_RELATIVE_PATH)
    if _is_valid_wheelhouse(wheelhouse_path):
        return CudaRuntimeInstallSource(
            mode="bundled-wheelhouse",
            pip_args=("--no-index", "--find-links", str(wheelhouse_path)),
            location=str(wheelhouse_path),
        )

    index_url = str(_read_optional_env_value(_CUDA_INDEX_URL_ENV) or _DEFAULT_CUDA_INDEX_URL).strip()
    extra_index_url = _read_optional_env_value(_CUDA_EXTRA_INDEX_URL_ENV)
    pip_args: list[str] = ["--index-url", index_url]
    location = index_url
    if extra_index_url:
        pip_args.extend(["--extra-index-url", extra_index_url])
        location = f"{index_url} | extra={extra_index_url}"

    return CudaRuntimeInstallSource(
        mode="configured-index",
        pip_args=tuple(pip_args),
        location=location,
    )


def resolve_installer_python_executable() -> Path:
    configured_python = _read_optional_env_path(_INSTALLER_PYTHON_ENV)
    if configured_python is not None:
        if configured_python.is_file():
            return configured_python
        raise RuntimeError(
            "Installer Python executable configured via "
            f"{_INSTALLER_PYTHON_ENV}, but the file does not exist: {configured_python}"
        )

    if not is_frozen_runtime():
        return Path(sys.executable).resolve()

    executable_path = Path(sys.executable).resolve()
    candidates = [
        executable_path.with_name("python.exe"),
        executable_path.parent / "runtime" / "python" / "python.exe",
        executable_path.parent / "_internal" / "python.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate

    raise RuntimeError(
        "Frozen installer runtime requires a dedicated Python executable with pip support. "
        "Bundle python.exe next to the app (or under runtime/python/) or configure "
        f"{_INSTALLER_PYTHON_ENV}."
    )


def build_cuda_runtime_install_command(
    request: CudaRuntimeInstallRequest,
    source: CudaRuntimeInstallSource,
    python_executable: Path,
) -> list[str]:
    return [
        str(python_executable),
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--disable-pip-version-check",
        "--no-warn-script-location",
        "--target",
        request.install_target,
        *source.pip_args,
        *request.packages,
    ]


def ensure_cuda_runtime_installed(
    request: CudaRuntimeInstallRequest,
    emit_event,
    cancel_event: threading.Event,
) -> None:
    install_target = Path(request.install_target)
    install_target.mkdir(parents=True, exist_ok=True)

    source = resolve_cuda_runtime_install_source()
    python_executable = resolve_installer_python_executable()
    install_command = build_cuda_runtime_install_command(request, source, python_executable)
    diagnostics = BoundedLineBuffer(max_lines=200)
    reporter = _InstallerStatusReporter(request, source, diagnostics, emit_event)

    logger.info(
        "CUDA installer subsystem starting | runtime_mode=%s | source_mode=%s | source_location=%s | python=%s | target=%s | packages=%s",
        get_runtime_mode_label(),
        source.mode,
        source.location,
        python_executable,
        install_target,
        ", ".join(request.packages) or "<none>",
    )
    reporter.emit("Installing GPU runtime...", include_tail=False, force=True)

    if cancel_event.is_set():
        raise CudaRuntimeInstallCanceledError("Installation canceled before launch.")

    if request.packages:
        _run_install_command(
            install_command=install_command,
            reporter=reporter,
            diagnostics=diagnostics,
            cancel_event=cancel_event,
        )
    else:
        logger.info("CUDA installer subsystem detected an empty package set; validating existing runtime only")

    missing_packages = get_missing_windows_cuda_runtime_packages()
    if missing_packages:
        raise RuntimeError(
            "GPU runtime installation finished, but required CUDA libraries are still missing:\n"
            + "\n".join(missing_packages)
        )

    reporter.emit("GPU runtime installed.", force=True)
    emit_event(build_finished_event())
    logger.info(
        "CUDA installer subsystem finished successfully | source_mode=%s | target=%s",
        source.mode,
        install_target,
    )


def build_cuda_runtime_failure_event(exc: BaseException, diagnostics_text: str = "") -> dict:
    diagnostics = str(diagnostics_text or "").strip()
    exc_text = f"{type(exc).__name__}: {exc}"
    if diagnostics:
        diagnostics = f"{exc_text}\n{diagnostics}"
    else:
        diagnostics = exc_text
    return build_failed_event("Failed to install GPU runtime.", diagnostics)


def _run_install_command(
    install_command: list[str],
    reporter: _InstallerStatusReporter,
    diagnostics: BoundedLineBuffer,
    cancel_event: threading.Event,
):
    process = subprocess.Popen(
        install_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    stdout_thread = threading.Thread(
        target=_collect_process_output,
        args=(process.stdout, diagnostics, reporter),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_collect_process_output,
        args=(process.stderr, diagnostics, reporter),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    try:
        while True:
            if cancel_event.is_set():
                logger.warning("CUDA installer subsystem cancel requested; terminating active installer process")
                _terminate_install_process(process)
                raise CudaRuntimeInstallCanceledError("Installation canceled by request.")

            return_code = process.poll()
            if return_code is not None:
                break
            time.sleep(0.2)
    finally:
        if stdout_thread.is_alive():
            stdout_thread.join(timeout=0.5)
        if stderr_thread.is_alive():
            stderr_thread.join(timeout=0.5)
        _close_stream(process.stdout)
        _close_stream(process.stderr)

    if int(return_code) != 0:
        raise RuntimeError(
            f"Installer command failed with exit code {return_code}.\n"
            + diagnostics.consume_text()
        )


def _collect_process_output(stream, diagnostics: BoundedLineBuffer, reporter: _InstallerStatusReporter):
    if stream is None:
        return
    try:
        for raw_line in stream:
            line = str(raw_line or "").rstrip()
            if not line:
                continue
            diagnostics.append(line)
            reporter.emit("Installing GPU runtime...")
    finally:
        _close_stream(stream)


def _terminate_install_process(process: subprocess.Popen[str]):
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=1.5)
        return
    except subprocess.TimeoutExpired:
        logger.warning("CUDA installer subprocess did not terminate gracefully; killing it")
    process.kill()
    process.wait(timeout=1.0)


def _close_stream(stream):
    if stream is None:
        return
    try:
        stream.close()
    except OSError:
        logger.debug("Best-effort installer stream close failed", exc_info=True)


def _read_optional_env_path(variable_name: str) -> Path | None:
    raw_value = _read_optional_env_value(variable_name)
    if not raw_value:
        return None
    return Path(raw_value).expanduser().resolve()


def _read_optional_env_value(variable_name: str) -> str | None:
    raw_value = str(os.environ.get(variable_name, "") or "").strip()
    return raw_value or None


def _is_valid_wheelhouse(path: Path) -> bool:
    try:
        return path.is_dir() and any(candidate.suffix.lower() == ".whl" for candidate in path.iterdir())
    except OSError:
        logger.warning("Unable to inspect CUDA wheelhouse path | path=%s", path, exc_info=True)
        return False
