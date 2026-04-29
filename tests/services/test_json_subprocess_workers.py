import importlib.util
import json
import logging
import subprocess
import sys
import threading
from pathlib import Path

from PySide6.QtWidgets import QApplication

from models.SubtitleGenerationDialogResult import SubtitleGenerationDialogResult
from services.runtime.RuntimeExecution import RuntimeLaunchSpec
from services.runtime.RuntimeInstallerProtocol import (
    EVENT_CANCELED as CUDA_EVENT_CANCELED,
    EVENT_FINISHED as CUDA_EVENT_FINISHED,
    EVENT_STATUS,
)


def _load_real_module(module_name: str, relative_path: str):
    project_root = Path(__file__).resolve().parents[2]
    module_path = project_root / relative_path
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _subtitle_options() -> SubtitleGenerationDialogResult:
    return SubtitleGenerationDialogResult(
        audio_stream_index=None,
        audio_language=None,
        device=None,
        model_size="small",
        output_format="srt",
        output_path="C:/tmp/out.srt",
        auto_open_after_generation=True,
    )


class _FakeStdin:
    def __init__(self):
        self.text = ""
        self.closed = False

    def write(self, text):
        self.text += str(text)

    def flush(self):
        return None

    def close(self):
        self.closed = True


class _FakeStream:
    def __init__(self, lines):
        self._buffer = "".join(lines)
        self._offset = 0
        self.closed = False

    def __iter__(self):
        while True:
            line = self.readline()
            if line == "":
                return
            yield line

    def readline(self, size=-1):
        if self._offset >= len(self._buffer):
            return ""

        newline_index = self._buffer.find("\n", self._offset)
        if newline_index == -1:
            end = len(self._buffer)
        else:
            end = newline_index + 1

        if size is not None and size >= 0:
            end = min(end, self._offset + size)

        line = self._buffer[self._offset:end]
        self._offset = end
        return line

    def close(self):
        self.closed = True


class _FakeProcess:
    def __init__(self, *, stdout=(), stderr=(), returncode=0, pid=4321, on_wait=None):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream(stdout)
        self.stderr = _FakeStream(stderr)
        self.returncode = None
        self._final_returncode = returncode
        self.pid = pid
        self.wait_calls = 0
        self._on_wait = on_wait

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self._on_wait is not None:
            self._on_wait()
        self.returncode = self._final_returncode
        return self.returncode

    def poll(self):
        return self.returncode


class _AliveProcess:
    pid = 9876

    def poll(self):
        return None


class _StuckProcess:
    def __init__(self, pid=9876):
        self.pid = pid
        self.returncode = None
        self.terminate_calls = 0
        self.sent_signals = []
        self.wait_timeouts = []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1

    def send_signal(self, signal_value):
        self.sent_signals.append(signal_value)

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        raise subprocess.TimeoutExpired(["fake"], timeout)


def _launch_spec():
    return RuntimeLaunchSpec(
        runtime_kind="test",
        runtime_name="test-runtime",
        command=["python", "-m", "fake"],
        cwd=None,
        execution_mode="test",
    )


def _install_fake_popen(monkeypatch, process):
    import services.runtime.JsonSubprocessWorker as base_module

    calls = []

    def fake_popen(*args, **kwargs):
        calls.append((args, kwargs))
        return process

    monkeypatch.setattr(base_module.subprocess, "Popen", fake_popen)
    return calls


def test_subtitle_worker_buffers_invalid_json_and_still_finishes(monkeypatch):
    module = _load_real_module(
        "real_subtitle_generation_workers_json_lifecycle_test",
        "services/subtitles/workers/SubtitleGenerationWorkers.py",
    )
    process = _FakeProcess(
        stdout=[
            "not-json\n",
            json.dumps({"event": module.EVENT_FINISHED, "output_path": "C:/tmp/out.srt", "auto_open": True}) + "\n",
        ],
        returncode=0,
    )
    _install_fake_popen(monkeypatch, process)
    monkeypatch.setattr(module, "build_runtime_helper_launch", lambda _helper: _launch_spec())

    worker = module.SubtitleGenerationWorker(3, "C:/media/movie.mkv", _subtitle_options())
    finished = []
    failed = []
    worker.finished.connect(lambda path, auto_open, fallback: finished.append((path, auto_open, fallback)))
    worker.failed.connect(lambda message, diagnostics: failed.append((message, diagnostics)))

    worker.run()

    assert finished == [("C:/tmp/out.srt", True, False)]
    assert failed == []
    assert "Invalid stdout event: not-json" in worker._stderr_buffer.consume_text()
    assert process.stdin.closed is True


def test_json_stdout_reader_bounds_oversized_event_lines(monkeypatch):
    module = _load_real_module(
        "real_subtitle_generation_workers_json_line_limit_test",
        "services/subtitles/workers/SubtitleGenerationWorkers.py",
    )
    process = _FakeProcess(
        stdout=[
            "x" * (module.JsonSubprocessWorkerBase._MAX_STDOUT_EVENT_LINE_CHARS + 10),
            "\n",
            json.dumps({"event": module.EVENT_FINISHED, "output_path": "C:/tmp/out.srt", "auto_open": True}) + "\n",
        ],
        returncode=0,
    )
    _install_fake_popen(monkeypatch, process)
    monkeypatch.setattr(module, "build_runtime_helper_launch", lambda _helper: _launch_spec())

    worker = module.SubtitleGenerationWorker(3, "C:/media/movie.mkv", _subtitle_options())
    finished = []
    worker.finished.connect(lambda path, auto_open, fallback: finished.append((path, auto_open, fallback)))

    worker.run()

    diagnostics = worker._stderr_buffer.consume_text()
    assert "stdout event exceeded" in diagnostics
    assert "x" * 1000 not in diagnostics
    assert finished == [("C:/tmp/out.srt", True, False)]


def test_cuda_worker_buffers_invalid_json_and_still_finishes(monkeypatch):
    import services.runtime.CudaRuntimeInstallWorker as module

    monkeypatch.setattr(module, "resolve_cuda_runtime_install_target", lambda: "C:/tmp/cuda-target")
    process = _FakeProcess(
        stdout=[
            "bad-status\n",
            json.dumps({"event": CUDA_EVENT_FINISHED}) + "\n",
        ],
        returncode=0,
    )
    _install_fake_popen(monkeypatch, process)
    monkeypatch.setattr(module, "build_runtime_installer_launch", lambda _installer: _launch_spec())

    worker = module.CudaRuntimeInstallWorker(["nvidia-cuda-nvrtc-cu12==12.9.86"])
    finished = []
    failed = []
    worker.finished.connect(lambda: finished.append(True))
    worker.failed.connect(lambda message: failed.append(message))

    worker.run()
    QApplication.processEvents()

    assert finished == [True]
    assert failed == []
    assert "Invalid stdout event: bad-status" in worker._stdout_buffer.consume_text()
    assert process.stdin.closed is True


def test_cuda_worker_reports_target_resolution_failure_from_run(monkeypatch):
    import services.runtime.CudaRuntimeInstallWorker as module

    monkeypatch.setattr(module, "resolve_cuda_runtime_install_target", lambda: (_ for _ in ()).throw(PermissionError("runtime denied")))

    worker = module.CudaRuntimeInstallWorker(["pkg"])
    failed = []
    worker.failed.connect(lambda message: failed.append(message))

    worker.run()

    assert len(failed) == 1
    assert "GPU runtime installation failed to start." in failed[0]
    assert "PermissionError: runtime denied" in failed[0]


def test_whisper_worker_reports_target_resolution_failure_from_run(monkeypatch):
    import services.runtime.WhisperModelInstallWorker as module

    monkeypatch.setattr(module, "resolve_whisper_model_install_target", lambda _model: (_ for _ in ()).throw(PermissionError("models denied")))

    worker = module.WhisperModelInstallWorker("small")
    failed = []
    worker.failed.connect(lambda message: failed.append(message))

    worker.run()

    assert len(failed) == 1
    assert "Whisper model installation failed to start." in failed[0]
    assert "PermissionError: models denied" in failed[0]


def test_known_terminal_events_emit_worker_signals(monkeypatch):
    import services.runtime.CudaRuntimeInstallWorker as cuda_module

    subtitle_module = _load_real_module(
        "real_subtitle_generation_workers_terminal_event_test",
        "services/subtitles/workers/SubtitleGenerationWorkers.py",
    )

    subtitle_worker = subtitle_module.SubtitleGenerationWorker(4, "C:/media/movie.mkv", _subtitle_options())
    subtitle_finished = []
    subtitle_worker.finished.connect(lambda path, auto_open, fallback: subtitle_finished.append((path, auto_open, fallback)))
    subtitle_worker._handle_event_line(
        json.dumps(
            {
                "event": subtitle_module.EVENT_FINISHED,
                "output_path": "C:/tmp/sub.srt",
                "auto_open": False,
                "used_fallback_output_path": True,
            }
        )
    )

    monkeypatch.setattr(cuda_module, "resolve_cuda_runtime_install_target", lambda: "C:/tmp/cuda-target")
    cuda_worker = cuda_module.CudaRuntimeInstallWorker(["pkg"])
    statuses = []
    canceled = []
    cuda_worker.status_changed.connect(lambda text: statuses.append(text))
    cuda_worker.canceled.connect(lambda: canceled.append(True))
    cuda_worker._handle_event_line(json.dumps({"event": EVENT_STATUS, "status": "Installing", "details": "Step 1"}))
    cuda_worker._handle_event_line(json.dumps({"event": CUDA_EVENT_CANCELED}))

    assert subtitle_finished == [("C:/tmp/sub.srt", False, True)]
    assert statuses == ["Installing"]
    assert canceled == [True]


def test_subtitle_progress_event_invalid_value_is_diagnostic(monkeypatch):
    module = _load_real_module(
        "real_subtitle_generation_workers_progress_payload_test",
        "services/subtitles/workers/SubtitleGenerationWorkers.py",
    )
    worker = module.SubtitleGenerationWorker(4, "C:/media/movie.mkv", _subtitle_options())
    progress_values = []
    worker.progress_changed.connect(progress_values.append)

    worker._handle_event_line(json.dumps({"event": module.EVENT_PROGRESS, "progress": "not-a-number"}))
    worker._handle_event_line(json.dumps({"event": module.EVENT_PROGRESS, "progress": 250}))

    assert progress_values == [0, 100]
    assert "Invalid progress event" in worker._stderr_buffer.consume_text()


def test_subtitle_exit_without_terminal_event_fails_or_cancels(monkeypatch):
    module = _load_real_module(
        "real_subtitle_generation_workers_missing_terminal_test",
        "services/subtitles/workers/SubtitleGenerationWorkers.py",
    )
    process = _FakeProcess(stdout=[], stderr=["helper stderr\n"], returncode=2)
    _install_fake_popen(monkeypatch, process)
    monkeypatch.setattr(module, "build_runtime_helper_launch", lambda _helper: _launch_spec())

    worker = module.SubtitleGenerationWorker(5, "C:/media/movie.mkv", _subtitle_options())
    failed = []
    worker.failed.connect(lambda message, diagnostics: failed.append((message, diagnostics)))

    worker.run()

    assert failed == [("Subtitle generation stopped unexpectedly.", "returncode=2\n\nhelper stderr")]

    canceled_worker = module.SubtitleGenerationWorker(6, "C:/media/movie.mkv", _subtitle_options())
    canceled = []
    canceled_worker.canceled.connect(lambda: canceled.append(True))
    canceling_process = _FakeProcess(
        stdout=[],
        stderr=[],
        returncode=1,
        on_wait=canceled_worker._request_cancel,
    )
    _install_fake_popen(monkeypatch, canceling_process)
    canceled_worker.run()

    assert canceled == [True]


def test_subtitle_subprocess_exception_after_cancel_emits_canceled(monkeypatch):
    module = _load_real_module(
        "real_subtitle_generation_workers_exception_cancel_test",
        "services/subtitles/workers/SubtitleGenerationWorkers.py",
    )
    monkeypatch.setattr(module, "build_runtime_helper_launch", lambda _helper: _launch_spec())

    worker = module.SubtitleGenerationWorker(6, "C:/media/movie.mkv", _subtitle_options())
    failed = []
    canceled = []
    worker.failed.connect(lambda message, diagnostics: failed.append((message, diagnostics)))
    worker.canceled.connect(lambda: canceled.append(True))
    worker._request_cancel()
    monkeypatch.setattr(worker, "_run_json_subprocess", lambda **_kwargs: (_ for _ in ()).throw(OSError("closed")))

    worker.run()

    assert failed == []
    assert canceled == [True]


def test_cuda_exit_without_terminal_event_includes_diagnostics(monkeypatch):
    import services.runtime.CudaRuntimeInstallWorker as module

    monkeypatch.setattr(module, "resolve_cuda_runtime_install_target", lambda: "C:/tmp/cuda-target")
    process = _FakeProcess(stdout=["stdout tail\n"], stderr=["stderr tail\n"], returncode=9)
    _install_fake_popen(monkeypatch, process)
    monkeypatch.setattr(module, "build_runtime_installer_launch", lambda _installer: _launch_spec())

    worker = module.CudaRuntimeInstallWorker(["pkg"])
    failed = []
    worker.failed.connect(lambda message: failed.append(message))

    worker.run()

    assert len(failed) == 1
    assert "returncode=9" in failed[0]
    assert "stderr tail" in failed[0]
    assert "stdout tail" in failed[0]

    canceled_worker = module.CudaRuntimeInstallWorker(["pkg"])
    canceled = []
    canceled_worker.canceled.connect(lambda: canceled.append(True))
    canceling_process = _FakeProcess(stdout=[], stderr=[], returncode=1, on_wait=canceled_worker._request_cancel)
    _install_fake_popen(monkeypatch, canceling_process)

    canceled_worker.run()
    QApplication.processEvents()

    assert canceled == [True]


def test_repeated_cancel_is_idempotent_and_force_stop_uses_background_termination(monkeypatch):
    import services.runtime.CudaRuntimeInstallWorker as cuda_module

    monkeypatch.setattr(cuda_module, "resolve_cuda_runtime_install_target", lambda: "C:/tmp/cuda-target")
    subtitle_module = _load_real_module(
        "real_subtitle_generation_workers_stop_test",
        "services/subtitles/workers/SubtitleGenerationWorkers.py",
    )

    subtitle_worker = subtitle_module.SubtitleGenerationWorker(7, "C:/media/movie.mkv", _subtitle_options())
    subtitle_begin_calls = []
    monkeypatch.setattr(subtitle_worker, "_begin_termination", lambda: subtitle_begin_calls.append(True))
    subtitle_worker.cancel()
    subtitle_worker.cancel()
    assert subtitle_begin_calls == [True]

    cuda_worker = cuda_module.CudaRuntimeInstallWorker(["pkg"])
    cuda_begin_calls = []
    kill_calls = []
    cuda_worker._set_active_process(_AliveProcess())
    monkeypatch.setattr(cuda_worker, "_begin_termination", lambda: cuda_begin_calls.append(True))
    monkeypatch.setattr(cuda_worker, "_kill_process_tree", lambda process: kill_calls.append(process.pid))

    cuda_worker.force_stop()
    cuda_worker.force_stop()

    assert kill_calls == []
    assert cuda_begin_calls == [True, True]


def test_worker_stop_methods_are_direct_thread_safe_requests(monkeypatch):
    import services.runtime.CudaRuntimeInstallWorker as cuda_module

    monkeypatch.setattr(cuda_module, "resolve_cuda_runtime_install_target", lambda: "C:/tmp/cuda-target")
    subtitle_module = _load_real_module(
        "real_subtitle_generation_workers_direct_stop_test",
        "services/subtitles/workers/SubtitleGenerationWorkers.py",
    )

    subtitle_worker = subtitle_module.SubtitleGenerationWorker(8, "C:/media/movie.mkv", _subtitle_options())
    subtitle_statuses = []
    subtitle_details = []
    subtitle_begin_calls = []
    subtitle_worker.status_changed.connect(lambda text: subtitle_statuses.append(text))
    subtitle_worker.details_changed.connect(lambda text: subtitle_details.append(text))
    subtitle_worker._set_active_process(_AliveProcess())
    monkeypatch.setattr(subtitle_worker, "_begin_termination", lambda: subtitle_begin_calls.append(True))

    subtitle_worker.cancel()
    subtitle_worker.force_stop()

    assert subtitle_worker._is_cancel_requested() is True
    assert subtitle_worker._is_force_stop_requested() is True
    assert subtitle_begin_calls == [True, True]
    assert subtitle_statuses == []
    assert subtitle_details == []

    cuda_worker = cuda_module.CudaRuntimeInstallWorker(["pkg"])
    cuda_statuses = []
    cuda_details = []
    cuda_begin_calls = []
    cuda_worker.status_changed.connect(lambda text: cuda_statuses.append(text))
    cuda_worker.details_changed.connect(lambda text: cuda_details.append(text))
    cuda_worker._set_active_process(_AliveProcess())
    monkeypatch.setattr(cuda_worker, "_begin_termination", lambda: cuda_begin_calls.append(True))

    cuda_worker.cancel()
    cuda_worker.force_stop()

    assert cuda_worker._is_cancel_requested() is True
    assert cuda_worker._is_force_stop_requested() is True
    assert cuda_begin_calls == [True, True]
    assert cuda_statuses == []
    assert cuda_details == []

    audio_worker = subtitle_module.AudioStreamProbeWorker(12, "C:/media/movie.mkv")
    audio_begin_calls = []
    audio_worker._set_active_process(_AliveProcess())
    monkeypatch.setattr(audio_worker, "_begin_termination", lambda: audio_begin_calls.append(True))

    audio_worker.cancel()
    audio_worker.force_stop()

    assert audio_worker._is_cancel_requested() is True
    assert audio_worker._is_force_stop_requested() is True
    assert audio_begin_calls == [True, True]


def test_subtitle_and_cuda_cancel_timeout_kills_helper_process(monkeypatch):
    import services.runtime.CudaRuntimeInstallWorker as cuda_module

    monkeypatch.setattr(cuda_module, "resolve_cuda_runtime_install_target", lambda: "C:/tmp/cuda-target")
    subtitle_module = _load_real_module(
        "real_subtitle_generation_workers_cancel_timeout_test",
        "services/subtitles/workers/SubtitleGenerationWorkers.py",
    )
    workers = [
        subtitle_module.SubtitleGenerationWorker(9, "C:/media/movie.mkv", _subtitle_options()),
        cuda_module.CudaRuntimeInstallWorker(["pkg"]),
    ]

    for worker in workers:
        process = _StuckProcess()
        kill_calls = []
        monkeypatch.setattr(worker, "_begin_termination", lambda: None)
        monkeypatch.setattr(worker, "_kill_process_tree", lambda stopped_process: kill_calls.append(stopped_process.pid))

        worker._set_active_process(process)
        worker.cancel()
        worker._terminate_process_lifecycle(process)

        assert worker._is_cancel_requested() is True
        assert process.wait_timeouts == [worker._graceful_cancel_timeout_seconds()]
        assert kill_calls == [process.pid]


def test_json_subprocess_reader_join_timeout_is_logged(caplog):
    import services.runtime.CudaRuntimeInstallWorker as module

    worker = module.CudaRuntimeInstallWorker(["pkg"])
    stuck_thread = threading.Thread(target=lambda: None)
    stuck_thread.name = "test stuck reader"
    stuck_thread.is_alive = lambda: True
    stuck_thread.join = lambda timeout=None: None

    with caplog.at_level(logging.WARNING):
        worker._join_json_subprocess_reader(stuck_thread, timeout=0.5, stream_name="stderr")

    assert "Cuda runtime installer process stderr reader did not stop within 0.5s" in caplog.text
