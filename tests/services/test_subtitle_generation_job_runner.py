from PySide6.QtCore import QObject, QEventLoop, QThread, QTimer, Signal, Slot, Qt

from models.SubtitleGenerationDialogResult import SubtitleGenerationDialogResult
from services.subtitles.workers import SubtitleGenerationJobRunner as runner_module
from services.subtitles.workers.SubtitleGenerationJobRunner import (
    _SubtitleWorkerSignalBridge,
    SubtitleGenerationJobRunner,
    SubtitleWorkerEventCallbacks,
    SubtitleWorkerLaunchCallbacks,
    can_launch_subtitle_worker_run,
)
from services.subtitles.state.SubtitlePipelineState import (
    SubtitleGenerationContext,
    SubtitlePipelinePhase,
    SubtitlePipelineRun,
)


class _FakeSignal:
    def __init__(self):
        self.connections = []

    def connect(self, callback, *_args):
        self.connections.append(callback)


class _FakeThread:
    instances = []

    def __init__(self, _parent):
        self.started = _FakeSignal()
        self.finished = _FakeSignal()
        self.start_calls = 0
        self.quit_calls = 0
        self.delete_later_calls = 0
        self._running = False
        self.__class__.instances.append(self)

    def isRunning(self):
        return self._running

    def start(self):
        self.start_calls += 1
        self._running = True

    def quit(self):
        self.quit_calls += 1

    def deleteLater(self):
        self.delete_later_calls += 1


class _FakeWorker:
    instances = []

    def __init__(self, run_id, media_path, options):
        self.run_id = run_id
        self.media_path = media_path
        self.options = options
        self.status_changed = _FakeSignal()
        self.progress_changed = _FakeSignal()
        self.details_changed = _FakeSignal()
        self.finished = _FakeSignal()
        self.failed = _FakeSignal()
        self.canceled = _FakeSignal()
        self.move_to_thread_calls = []
        self.delete_later_calls = 0
        self.__class__.instances.append(self)

    def moveToThread(self, thread):
        self.move_to_thread_calls.append(thread)

    def run(self):
        pass

    def deleteLater(self):
        self.delete_later_calls += 1


class _QtEmitter(QObject):
    progress = Signal(int)
    finished = Signal()

    @Slot()
    def run(self):
        self.progress.emit(42)
        self.finished.emit()


def _options() -> SubtitleGenerationDialogResult:
    return SubtitleGenerationDialogResult(
        audio_stream_index=None,
        audio_language=None,
        device=None,
        model_size="small",
        output_format="srt",
        output_path="C:/media/movie.srt",
        auto_open_after_generation=True,
    )


def _run() -> SubtitlePipelineRun:
    return SubtitlePipelineRun(
        run_id=7,
        context=SubtitleGenerationContext(media_path="C:/media/movie.mkv", request_id=3),
        requested_options=_options(),
        phase=SubtitlePipelinePhase.RUNNING,
    )


def test_can_launch_subtitle_worker_run_checks_refs_and_phase():
    run = _run()
    thread = object()
    worker = object()
    run.subtitle_thread = thread
    run.subtitle_worker = worker

    assert can_launch_subtitle_worker_run(run, thread, worker) is True

    run.phase = SubtitlePipelinePhase.SUCCEEDED

    assert can_launch_subtitle_worker_run(run, thread, worker) is False
    assert can_launch_subtitle_worker_run(run, object(), worker) is False


def test_worker_signal_bridge_dispatches_queued_worker_signal_on_main_thread():
    thread = QThread()
    emitter = _QtEmitter()
    emitter.moveToThread(thread)
    callback_threads = []
    loop = QEventLoop()
    worker_identity = object()
    callbacks = SubtitleWorkerEventCallbacks(
        on_status_changed=lambda run_id, worker, text: None,
        on_progress_changed=lambda run_id, worker, value: callback_threads.append(QThread.isMainThread()),
        on_details_changed=lambda run_id, worker, text: None,
        on_finished=lambda run_id, worker, path, auto_open, fallback: None,
        on_failed=lambda run_id, worker, error, diagnostics: None,
        on_canceled=lambda run_id, worker: None,
    )
    bridge = _SubtitleWorkerSignalBridge(
        run_id=7,
        worker=worker_identity,
        callbacks=callbacks,
        parent=None,
    )

    thread.started.connect(emitter.run)
    emitter.progress.connect(bridge.on_progress_changed, Qt.QueuedConnection)
    emitter.finished.connect(thread.quit)
    thread.finished.connect(loop.quit)
    QTimer.singleShot(2000, loop.quit)

    thread.start()
    loop.exec()

    thread.wait(1000)
    emitter.deleteLater()
    bridge.deleteLater()
    assert callback_threads == [True]


def test_job_runner_wires_worker_and_starts_after_deferred_validation(monkeypatch):
    _FakeThread.instances = []
    _FakeWorker.instances = []
    single_shots = []
    can_start_calls = []
    callback_calls = []
    suspend_calls = []
    callbacks = {
        "start_aborted": lambda run_id, thread, worker: None,
        "status": lambda run_id, worker, text: callback_calls.append(("status", run_id, worker, text)),
        "progress": lambda run_id, worker, value: callback_calls.append(("progress", run_id, worker, value)),
        "details": lambda run_id, worker, text: callback_calls.append(("details", run_id, worker, text)),
        "finished": lambda run_id, worker, path, auto_open, fallback: callback_calls.append(
            ("finished", run_id, worker, path, auto_open, fallback)
        ),
        "failed": lambda run_id, worker, error, diagnostics: callback_calls.append(
            ("failed", run_id, worker, error, diagnostics)
        ),
        "canceled": lambda run_id, worker: callback_calls.append(("canceled", run_id, worker)),
    }

    def fake_single_shot(_delay, callback):
        single_shots.append(callback)
        callback()

    def can_start(run_id, thread, worker):
        can_start_calls.append((run_id, thread, worker))
        return True

    monkeypatch.setattr(runner_module, "QThread", _FakeThread)
    monkeypatch.setattr(runner_module, "SubtitleGenerationWorker", _FakeWorker)
    monkeypatch.setattr(runner_module.QTimer, "singleShot", fake_single_shot)

    runner = SubtitleGenerationJobRunner(
        None,
        launch_callbacks=SubtitleWorkerLaunchCallbacks(
            can_start_worker=can_start,
            on_start_aborted=callbacks["start_aborted"],
            suspend_before_start=lambda: suspend_calls.append(True),
        ),
        event_callbacks=SubtitleWorkerEventCallbacks(
            on_status_changed=callbacks["status"],
            on_progress_changed=callbacks["progress"],
            on_details_changed=callbacks["details"],
            on_finished=callbacks["finished"],
            on_failed=callbacks["failed"],
            on_canceled=callbacks["canceled"],
        ),
    )
    run = _run()

    runner.start(run, _options())

    thread = _FakeThread.instances[0]
    worker = _FakeWorker.instances[0]
    assert run.subtitle_thread is thread
    assert run.subtitle_worker is worker
    assert run.subtitle_cancel_requested is False
    assert worker.move_to_thread_calls == [thread]
    assert len(worker.status_changed.connections) == 1
    assert len(worker.progress_changed.connections) == 1
    assert len(worker.details_changed.connections) == 1
    assert len(worker.finished.connections) == 2
    assert len(worker.failed.connections) == 2
    assert len(worker.canceled.connections) == 2
    worker.status_changed.connections[0]("Loading")
    worker.progress_changed.connections[0](42)
    worker.details_changed.connections[0]("Details")
    worker.finished.connections[0]("out.srt", True, False)
    worker.failed.connections[0]("error", "diagnostics")
    worker.canceled.connections[0]()
    assert callback_calls == [
        ("status", run.run_id, worker, "Loading"),
        ("progress", run.run_id, worker, 42),
        ("details", run.run_id, worker, "Details"),
        ("finished", run.run_id, worker, "out.srt", True, False),
        ("failed", run.run_id, worker, "error", "diagnostics"),
        ("canceled", run.run_id, worker),
    ]
    assert thread.started.connections == [worker.run]
    assert len(thread.finished.connections) == 4
    assert len(single_shots) == 1
    assert len(can_start_calls) == 1
    assert suspend_calls == [True]
    assert thread.start_calls == 1


def test_job_runner_cleans_up_when_deferred_start_is_rejected(monkeypatch):
    _FakeThread.instances = []
    _FakeWorker.instances = []
    aborted = []

    def fake_single_shot(_delay, callback):
        callback()

    monkeypatch.setattr(runner_module, "QThread", _FakeThread)
    monkeypatch.setattr(runner_module, "SubtitleGenerationWorker", _FakeWorker)
    monkeypatch.setattr(runner_module.QTimer, "singleShot", fake_single_shot)

    runner = SubtitleGenerationJobRunner(
        None,
        launch_callbacks=SubtitleWorkerLaunchCallbacks(
            can_start_worker=lambda _run_id, _thread, _worker: False,
            on_start_aborted=lambda run_id, thread, worker: aborted.append((run_id, thread, worker)),
            suspend_before_start=lambda: None,
        ),
        event_callbacks=SubtitleWorkerEventCallbacks(
            on_status_changed=lambda run_id, worker, text: None,
            on_progress_changed=lambda run_id, worker, value: None,
            on_details_changed=lambda run_id, worker, text: None,
            on_finished=lambda run_id, worker, path, auto_open, fallback: None,
            on_failed=lambda run_id, worker, error, diagnostics: None,
            on_canceled=lambda run_id, worker: None,
        ),
    )
    run = _run()

    runner.start(run, _options())

    thread = _FakeThread.instances[0]
    worker = _FakeWorker.instances[0]
    assert aborted == [(run.run_id, thread, worker)]
    assert thread.start_calls == 0
    assert worker.delete_later_calls == 1
    assert thread.delete_later_calls == 1
