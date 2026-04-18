from services.runtime.SubprocessWorkerSupport import CancelAwareWorkerMixin, SubprocessStopPolicyMixin


class _AliveProcess:
    pid = 1234

    def poll(self):
        return None


class _StopPolicyHarness(CancelAwareWorkerMixin, SubprocessStopPolicyMixin):
    def __init__(self, process=None):
        self._init_cancel_state()
        self._process = process
        self._force_stop_requested = False
        self.begin_calls = 0
        self.kill_calls = 0
        self.cancel_hooks = 0
        self.force_hooks = 0
        self.repeated_force_hooks = 0
        self.kill_failed_hooks = 0

    def _begin_termination(self):
        self.begin_calls += 1

    def _kill_process_tree(self, _process):
        self.kill_calls += 1

    def cancel_hook(self):
        self.cancel_hooks += 1

    def force_hook(self):
        self.force_hooks += 1

    def repeated_force_hook(self):
        self.repeated_force_hooks += 1

    def kill_failed_hook(self, _process):
        self.kill_failed_hooks += 1


def test_graceful_subprocess_stop_is_idempotent():
    worker = _StopPolicyHarness()

    assert worker._request_graceful_subprocess_stop(worker.cancel_hook) is True
    assert worker._request_graceful_subprocess_stop(worker.cancel_hook) is False

    assert worker._is_cancel_requested() is True
    assert worker.cancel_hooks == 1
    assert worker.begin_calls == 1


def test_force_subprocess_stop_after_cancel_escalates_to_kill_path_when_process_is_alive():
    process = _AliveProcess()
    worker = _StopPolicyHarness(process)

    assert worker._request_graceful_subprocess_stop(worker.cancel_hook) is True
    assert worker._request_force_subprocess_stop(
        worker.force_hook,
        worker.repeated_force_hook,
        worker.kill_failed_hook,
    ) is True

    assert worker._is_cancel_requested() is True
    assert worker._force_stop_requested is True
    assert worker.cancel_hooks == 1
    assert worker.force_hooks == 1
    assert worker.repeated_force_hooks == 0
    assert worker.kill_calls == 1
    assert worker.begin_calls == 2


def test_repeated_force_subprocess_stop_only_runs_repeated_hook():
    worker = _StopPolicyHarness(_AliveProcess())

    assert worker._request_force_subprocess_stop(
        worker.force_hook,
        worker.repeated_force_hook,
        worker.kill_failed_hook,
    ) is True
    assert worker._request_force_subprocess_stop(
        worker.force_hook,
        worker.repeated_force_hook,
        worker.kill_failed_hook,
    ) is False

    assert worker.force_hooks == 1
    assert worker.repeated_force_hooks == 1
    assert worker.kill_calls == 2
    assert worker.begin_calls == 2
