from services.subtitles.workers.SubtitleCudaRuntimeFlow import SubtitleCudaRuntimeFlow


def test_cuda_runtime_flow_treats_assigned_thread_as_active(qt_parent):
    flow = SubtitleCudaRuntimeFlow(qt_parent)
    flow._thread = object()

    assert flow.is_active() is True


def test_cuda_runtime_flow_ignores_stale_thread_finish(qt_parent):
    flow = SubtitleCudaRuntimeFlow(qt_parent)
    old_thread = object()
    new_thread = object()
    new_worker = object()
    emitted_run_ids = []
    flow.thread_finished.connect(emitted_run_ids.append)

    flow._thread = new_thread
    flow._worker = new_worker
    flow._cancel_requested = True
    flow._run_id = 2

    flow._on_thread_finished(1, old_thread)

    assert flow._thread is new_thread
    assert flow._worker is new_worker
    assert flow._cancel_requested is True
    assert flow._run_id == 2
    assert emitted_run_ids == []


def test_cuda_runtime_flow_clears_matching_thread_finish(qt_parent):
    flow = SubtitleCudaRuntimeFlow(qt_parent)
    thread = object()
    emitted_run_ids = []
    flow.thread_finished.connect(emitted_run_ids.append)

    flow._thread = thread
    flow._worker = object()
    flow._cancel_requested = True
    flow._run_id = 1

    flow._on_thread_finished(1, thread)

    assert flow._thread is None
    assert flow._worker is None
    assert flow._cancel_requested is False
    assert flow._run_id is None
    assert emitted_run_ids == [1]


def test_cuda_runtime_flow_delivers_terminal_event_after_thread_cleanup(qt_parent):
    flow = SubtitleCudaRuntimeFlow(qt_parent)
    thread = object()
    worker = object()
    finished_run_ids = []
    flow.finished.connect(finished_run_ids.append)

    flow._thread = thread
    flow._worker = worker
    flow._run_id = 3

    flow._on_thread_finished(3, thread)
    flow._on_worker_finished(3, worker)

    assert finished_run_ids == [3]
