from __future__ import annotations

import json
import logging
import subprocess
import threading
from dataclasses import dataclass
from typing import IO

from services.runtime.RuntimeExecution import RuntimeLaunchSpec
from services.runtime.SubprocessLifecycle import SubprocessLifecycleMixin
from services.runtime.SubprocessWorkerSupport import (
    CancelAwareWorkerMixin,
    SubprocessStopPolicyMixin,
    TerminalEventMixin,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JsonSubprocessRunResult:
    process: subprocess.Popen[str] | None
    return_code: int | None


class JsonSubprocessWorkerBase(
    SubprocessLifecycleMixin,
    CancelAwareWorkerMixin,
    SubprocessStopPolicyMixin,
    TerminalEventMixin,
):
    _MAX_STDOUT_EVENT_LINE_CHARS = 1_000_000

    def _init_json_subprocess_worker(self):
        self._init_subprocess_lifecycle()
        self._init_cancel_state()
        self._init_terminal_event_state()

    def _run_json_subprocess(
        self,
        *,
        launch_spec: RuntimeLaunchSpec,
        request_json: str,
        stderr_buffer,
        read_stdout_in_thread: bool = False,
    ) -> JsonSubprocessRunResult:
        process: subprocess.Popen[str] | None = None
        stdout_thread: threading.Thread | None = None
        stderr_thread: threading.Thread | None = None
        return_code: int | None = None

        try:
            process = self._spawn_json_subprocess(launch_spec)
            self._last_json_subprocess_process = process
            self._process = process
            self._after_json_subprocess_spawned(process, launch_spec)
            if self._is_cancel_requested():
                self._on_json_subprocess_deferred_cancel(process)
                self._begin_termination()

            if read_stdout_in_thread:
                stdout_thread = threading.Thread(
                    target=self._read_stdout_events,
                    args=(process,),
                    name=f"{self._subprocess_log_name()} stdout reader",
                    daemon=True,
                )
                stdout_thread.start()

            stderr_thread = threading.Thread(
                target=self._collect_stream,
                args=(process.stderr, stderr_buffer, "stderr"),
                name=f"{self._subprocess_log_name()} stderr reader",
                daemon=True,
            )
            stderr_thread.start()

            self._write_json_request(process, request_json)

            if not read_stdout_in_thread:
                self._read_stdout_events(process)

            return_code = process.wait()
            self._join_json_subprocess_reader(stderr_thread, timeout=1.0, stream_name="stderr")
            if stdout_thread is not None:
                self._join_json_subprocess_reader(stdout_thread, timeout=1.0, stream_name="stdout")
            return JsonSubprocessRunResult(process=process, return_code=return_code)
        finally:
            self._process = None
            if process is not None:
                self._close_stream(process.stdin)
                self._close_stream(process.stdout)
                self._close_stream(process.stderr)
            if stdout_thread is not None and stdout_thread.is_alive():
                self._join_json_subprocess_reader(stdout_thread, timeout=0.5, stream_name="stdout")
            if stderr_thread is not None and stderr_thread.is_alive():
                self._join_json_subprocess_reader(stderr_thread, timeout=0.5, stream_name="stderr")

    def _spawn_json_subprocess(self, launch_spec: RuntimeLaunchSpec) -> subprocess.Popen[str]:
        return subprocess.Popen(
            launch_spec.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=launch_spec.cwd,
            **self._subprocess_spawn_options(),
        )

    def _write_json_request(self, process: subprocess.Popen[str], request_json: str):
        if process.stdin is None:
            raise RuntimeError(f"{self._json_subprocess_display_name()} stdin is unavailable.")
        process.stdin.write(request_json)
        process.stdin.flush()
        process.stdin.close()

    def _read_stdout_events(self, process: subprocess.Popen[str]):
        if process.stdout is None:
            raise RuntimeError(f"{self._json_subprocess_display_name()} stdout is unavailable.")

        while True:
            raw_line = process.stdout.readline(self._MAX_STDOUT_EVENT_LINE_CHARS + 1)
            if raw_line == "":
                break
            if len(raw_line) > self._MAX_STDOUT_EVENT_LINE_CHARS:
                if not raw_line.endswith("\n"):
                    self._discard_oversized_stdout_line(process.stdout)
                self._handle_invalid_json_stdout(
                    f"stdout event exceeded {self._MAX_STDOUT_EVENT_LINE_CHARS} characters"
                )
                continue

            line = raw_line.strip()
            if not line:
                continue
            self._handle_event_line(line)

    def _discard_oversized_stdout_line(self, stream: IO[str]):
        while True:
            chunk = stream.readline(self._MAX_STDOUT_EVENT_LINE_CHARS)
            if chunk == "" or chunk.endswith("\n"):
                return

    def _handle_event_line(self, line: str):
        try:
            event = json.loads(line)
        except Exception:
            self._handle_invalid_json_stdout(line)
            return

        event_type = str(event.get("event") or "").strip().lower()
        self._handle_json_event(event_type, event, line)

    def _collect_stream(self, stream: IO[str] | None, target, stream_name: str):
        if stream is None:
            return
        try:
            for line in stream:
                text = line.rstrip()
                if text:
                    target.append(text)
        except OSError:
            if not self._is_cancel_requested():
                logger.debug("%s %s stream closed unexpectedly", self._subprocess_log_name(), stream_name)

    def _join_json_subprocess_reader(self, thread: threading.Thread, *, timeout: float, stream_name: str):
        thread.join(timeout=timeout)
        if thread.is_alive():
            logger.warning(
                "%s %s reader did not stop within %.1fs",
                self._subprocess_log_name().capitalize(),
                stream_name,
                timeout,
            )

    def _after_json_subprocess_spawned(self, process: subprocess.Popen[str], launch_spec: RuntimeLaunchSpec):
        return None

    def _on_json_subprocess_deferred_cancel(self, process: subprocess.Popen[str]):
        return None

    def _handle_invalid_json_stdout(self, line: str):
        raise NotImplementedError

    def _handle_json_event(self, event_type: str, event: dict, line: str):
        raise NotImplementedError

    def _json_subprocess_display_name(self) -> str:
        return self._subprocess_log_name()
