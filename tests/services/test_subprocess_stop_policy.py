from services.runtime.SubprocessLifecycle import SubprocessLifecycleMixin
from services.runtime.SubprocessWorkerSupport import CancelAwareWorkerMixin, SubprocessStopPolicyMixin


class _AliveProcess:
    def __init__(self, pid=1234):
        self.pid = pid

    def poll(self):
        return None


class _StopPolicyHarness(CancelAwareWorkerMixin, SubprocessLifecycleMixin, SubprocessStopPolicyMixin):
    def __init__(self, process=None):
        self._init_subprocess_lifecycle()
        self._init_cancel_state()
        self._set_active_process(process)
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


class _LifecycleHarness(SubprocessLifecycleMixin):
    def __init__(self, process=None):
        self._init_subprocess_lifecycle()
        self._set_active_process(process)
        self.terminated_processes = []

    def _request_graceful_stop(self, process):
        self.terminated_processes.append(process)

    def _kill_process_tree(self, process):
        self.terminated_processes.append(process)


def test_graceful_subprocess_stop_is_idempotent():
    worker = _StopPolicyHarness()

    assert worker._request_graceful_subprocess_stop(worker.cancel_hook) is True
    assert worker._request_graceful_subprocess_stop(worker.cancel_hook) is False

    assert worker._is_cancel_requested() is True
    assert worker.cancel_hooks == 1
    assert worker.begin_calls == 1


def test_force_subprocess_stop_after_cancel_escalates_via_background_termination():
    process = _AliveProcess()
    worker = _StopPolicyHarness(process)

    assert worker._request_graceful_subprocess_stop(worker.cancel_hook) is True
    assert worker._request_force_subprocess_stop(
        worker.force_hook,
        worker.repeated_force_hook,
        worker.kill_failed_hook,
    ) is True

    assert worker._is_cancel_requested() is True
    assert worker._is_force_stop_requested() is True
    assert worker.cancel_hooks == 1
    assert worker.force_hooks == 1
    assert worker.repeated_force_hooks == 0
    assert worker.kill_calls == 0
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
    assert worker.kill_calls == 0
    assert worker.begin_calls == 2


def test_subprocess_lifecycle_terminates_process_snapshot_only():
    first_process = _AliveProcess(pid=1)
    second_process = _AliveProcess(pid=2)
    worker = _LifecycleHarness(first_process)
    worker._mark_force_stop_requested()

    worker._terminating_process = first_process
    worker._termination_started = True
    worker._set_active_process(second_process)

    worker._terminate_process_lifecycle(first_process)

    assert worker.terminated_processes == [first_process]
    assert second_process not in worker.terminated_processes
    assert worker._termination_started is False
    assert worker._terminating_process is None


def test_subprocess_lifecycle_stale_completion_does_not_clear_active_termination():
    old_process = _AliveProcess(pid=1)
    active_process = _AliveProcess(pid=2)
    worker = _LifecycleHarness(active_process)
    worker._mark_force_stop_requested()
    worker._terminating_process = active_process
    worker._termination_started = True

    worker._terminate_process_lifecycle(old_process)

    assert worker.terminated_processes == [old_process]
    assert worker._termination_started is True
    assert worker._terminating_process is active_process
