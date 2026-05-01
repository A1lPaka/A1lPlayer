from services.subtitles.workers.SubtitleWhisperModelFlow import SubtitleWhisperModelFlow


def test_whisper_model_flow_treats_assigned_thread_as_active(qt_parent):
    flow = SubtitleWhisperModelFlow(qt_parent)
    flow._thread = object()

    assert flow.is_active() is True


def test_whisper_model_flow_delivers_terminal_event_after_thread_cleanup(qt_parent):
    flow = SubtitleWhisperModelFlow(qt_parent)
    thread = object()
    worker = object()
    canceled_run_ids = []
    flow.canceled.connect(canceled_run_ids.append)

    flow._thread = thread
    flow._worker = worker
    flow._run_id = 5

    flow._on_thread_finished(5, thread)
    flow._on_worker_canceled(5, worker)

    assert canceled_run_ids == [5]
