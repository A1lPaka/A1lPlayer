import threading
from collections import deque
from typing import Iterable


class CancelAwareWorkerMixin:
    def _init_cancel_state(self):
        self._cancel_event = threading.Event()

    def _is_cancel_requested(self) -> bool:
        return self._cancel_event.is_set()

    def _request_cancel(self) -> bool:
        if self._cancel_event.is_set():
            return False
        self._cancel_event.set()
        return True


class SubprocessStopPolicyMixin:
    def _request_graceful_subprocess_stop(self, on_cancel_requested) -> bool:
        if not self._request_cancel():
            return False

        on_cancel_requested()
        self._begin_termination()
        return True

    def _request_force_subprocess_stop(self, on_force_requested, on_repeated_force_stop, on_kill_failed) -> bool:
        first_request = not self._force_stop_requested
        if first_request:
            self._force_stop_requested = True
            self._request_cancel()
            on_force_requested()
        else:
            on_repeated_force_stop()

        process = self._process
        if process is not None and process.poll() is None:
            try:
                self._kill_process_tree(process)
            except Exception:
                on_kill_failed(process)

        self._begin_termination()
        return first_request


class TerminalEventMixin:
    def _init_terminal_event_state(self):
        self._terminal_event_lock = threading.Lock()
        self._terminal_event_emitted = False

    def _mark_terminal_event_emitted(self) -> bool:
        with self._terminal_event_lock:
            if self._terminal_event_emitted:
                return False
            self._terminal_event_emitted = True
            return True

    def _terminal_event_already_emitted(self) -> bool:
        with self._terminal_event_lock:
            return self._terminal_event_emitted


class BoundedLineBuffer:
    def __init__(self, max_lines: int):
        self._lines: deque[str] = deque(maxlen=max(1, int(max_lines)))
        self._lock = threading.Lock()

    def append(self, text: str):
        with self._lock:
            self._lines.append(str(text))

    def consume_text(self) -> str:
        with self._lock:
            return "\n".join(self._lines).strip()

    def tail(self, count: int) -> str:
        with self._lock:
            return "\n".join(list(self._lines)[-max(1, int(count)) :]).strip()


def build_process_diagnostics(return_code: int | None, sections: Iterable[tuple[str | None, str]]) -> str:
    parts = [f"returncode={return_code}"]
    for label, text in sections:
        normalized = str(text).strip()
        if not normalized:
            continue
        if label:
            parts.append(f"{label}:\n{normalized}")
        else:
            parts.append(normalized)
    return "\n\n".join(parts)


def build_exception_diagnostics(
    exc: BaseException,
    sections: Iterable[tuple[str | None, str]],
) -> str:
    parts = [f"{type(exc).__name__}: {exc}"]
    for label, text in sections:
        normalized = str(text).strip()
        if not normalized:
            continue
        if label:
            parts.append(f"{label}:\n{normalized}")
        else:
            parts.append(normalized)
    return "\n\n".join(parts)
