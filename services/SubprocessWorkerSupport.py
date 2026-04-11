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
        self._lines: deque[str] = deque(maxlen=max_lines)
        self._lock = threading.Lock()

    def append(self, text: str):
        with self._lock:
            self._lines.append(str(text))

    def consume_text(self) -> str:
        with self._lock:
            return "\n".join(self._lines).strip()


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
