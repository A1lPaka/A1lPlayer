from PySide6.QtCore import QObject

from services.runtime.QtWorkerInvoke import invoke_worker_method


class _PlainWorker:
    def __init__(self):
        self.calls = 0

    def cancel(self):
        self.calls += 1


class _QObjectWithoutSlot(QObject):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def cancel(self):
        self.calls += 1


def test_invoke_worker_method_falls_back_for_plain_test_doubles():
    worker = _PlainWorker()

    assert invoke_worker_method(worker, "cancel") is True

    assert worker.calls == 1


def test_invoke_worker_method_does_not_direct_call_qobject_without_slot():
    worker = _QObjectWithoutSlot()

    assert invoke_worker_method(worker, "cancel") is False

    assert worker.calls == 0
