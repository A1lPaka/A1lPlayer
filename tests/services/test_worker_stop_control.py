from PySide6.QtCore import QObject

from services.runtime.WorkerStopControl import call_worker_stop


class _PlainWorker:
    def __init__(self):
        self.calls = 0

    def cancel(self):
        self.calls += 1


class _QObjectWorker(QObject):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def cancel(self):
        self.calls += 1


def test_call_worker_stop_supports_plain_test_doubles():
    worker = _PlainWorker()

    assert call_worker_stop(worker, "cancel") is True

    assert worker.calls == 1


def test_call_worker_stop_calls_qobject_method_directly():
    worker = _QObjectWorker()

    assert call_worker_stop(worker, "cancel") is True

    assert worker.calls == 1


def test_call_worker_stop_reports_missing_method():
    worker = _PlainWorker()

    assert call_worker_stop(worker, "force_stop") is False

    assert worker.calls == 0
