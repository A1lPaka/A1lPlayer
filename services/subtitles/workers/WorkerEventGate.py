from __future__ import annotations

from dataclasses import dataclass


@dataclass
class WorkerEventGate:
    active_run_id: int | None = None
    active_worker: object | None = None
    finished_run_id: int | None = None
    finished_worker: object | None = None
    terminal_event_emitted: bool = False

    def start(self, run_id: int, worker: object) -> None:
        self.active_run_id = run_id
        self.active_worker = worker
        self.finished_run_id = None
        self.finished_worker = None
        self.terminal_event_emitted = False

    def accepts(self, run_id: int, worker: object, *, terminal: bool = False) -> bool:
        if terminal and self.finished_run_id == run_id and worker is self.finished_worker:
            return True
        return self.active_run_id == run_id and worker is self.active_worker

    def emit_if_current(self, run_id: int, worker: object, signal, *args, terminal: bool = False) -> bool:
        if not self.accepts(run_id, worker, terminal=terminal):
            return False
        if terminal:
            self.mark_terminal_emitted()
        signal.emit(run_id, *args)
        return True

    def mark_terminal_emitted(self) -> None:
        self.terminal_event_emitted = True
        self.finished_run_id = None
        self.finished_worker = None

    def cancel_active(self, run_id: int, worker: object) -> None:
        if self.active_run_id == run_id and worker is self.active_worker:
            self.active_run_id = None
            self.active_worker = None
            self.finished_run_id = None
            self.finished_worker = None
            self.terminal_event_emitted = False

    def finish_thread(self, run_id: int, worker: object | None) -> None:
        if not self.terminal_event_emitted:
            self.finished_run_id = run_id
            self.finished_worker = worker
        self.active_run_id = None
        self.active_worker = None
