from services.subtitles.workers.WorkerEventGate import WorkerEventGate


def test_worker_event_gate_accepts_terminal_event_after_thread_finish():
    gate = WorkerEventGate()
    worker = object()

    gate.start(7, worker)
    gate.finish_thread(7, worker)

    assert gate.accepts(7, worker, terminal=True) is True
    assert gate.accepts(7, worker, terminal=False) is False

    gate.mark_terminal_emitted()

    assert gate.accepts(7, worker, terminal=True) is False


def test_worker_event_gate_rejects_stale_worker_identity():
    gate = WorkerEventGate()
    worker = object()

    gate.start(7, worker)

    assert gate.accepts(8, worker, terminal=True) is False
    assert gate.accepts(7, object(), terminal=True) is False
