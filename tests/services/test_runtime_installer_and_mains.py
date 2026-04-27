from io import StringIO
import json
from types import SimpleNamespace
import threading

import pytest

from services.runtime import CudaRuntimeInstaller as installer
from services.runtime import RuntimeHelperMain as helper_main
from services.runtime import RuntimeInstallerMain as installer_main
from services.runtime.RuntimeExecution import EVENT_CANCELED, EVENT_FAILED, EVENT_FINISHED
from services.runtime.RuntimeHelperProtocol import SubtitleGenerationRequest
from services.runtime.RuntimeInstallerProtocol import (
    CudaRuntimeInstallRequest,
    EVENT_STATUS,
)
from services.subtitles.domain.SubtitleTypes import (
    SubtitleGenerationCanceledError,
    SubtitleSegment,
)


def _json_events(stdout: StringIO) -> list[dict]:
    return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]


def test_cuda_runtime_installer_reads_env_paths_and_index_urls(monkeypatch, workspace_tmp_path):
    wheelhouse = workspace_tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    (wheelhouse / "nvidia_runtime.whl").write_text("wheel", encoding="utf-8")

    monkeypatch.setenv("A1LPLAYER_CUDA_WHEELHOUSE", str(wheelhouse))

    source = installer.resolve_cuda_runtime_install_source()

    assert source.mode == "bundled-wheelhouse"
    assert source.pip_args == ("--no-index", "--find-links", str(wheelhouse.resolve()))
    assert source.location == str(wheelhouse.resolve())

    monkeypatch.delenv("A1LPLAYER_CUDA_WHEELHOUSE")
    monkeypatch.setenv("A1LPLAYER_CUDA_INDEX_URL", "https://example.invalid/simple")
    monkeypatch.setenv("A1LPLAYER_CUDA_EXTRA_INDEX_URL", "https://extra.invalid/simple")
    monkeypatch.setattr(installer, "resolve_runtime_app_root", lambda: workspace_tmp_path)

    source = installer.resolve_cuda_runtime_install_source()

    assert source.mode == "configured-index"
    assert source.pip_args == (
        "--index-url",
        "https://example.invalid/simple",
        "--extra-index-url",
        "https://extra.invalid/simple",
    )


def test_cuda_runtime_installer_validates_wheelhouse(workspace_tmp_path):
    empty_dir = workspace_tmp_path / "empty"
    empty_dir.mkdir()
    wheelhouse = workspace_tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    (wheelhouse / "package.txt").write_text("no wheel", encoding="utf-8")

    assert installer._is_valid_wheelhouse(empty_dir) is False
    assert installer._is_valid_wheelhouse(wheelhouse) is False

    (wheelhouse / "package.whl").write_text("wheel", encoding="utf-8")

    assert installer._is_valid_wheelhouse(wheelhouse) is True


def test_cuda_runtime_installer_builds_install_command():
    request = CudaRuntimeInstallRequest(
        packages=("nvidia-cublas-cu12", "nvidia-cudnn-cu12"),
        install_target="C:/runtime",
    )
    source = installer.CudaRuntimeInstallSource(
        mode="configured-index",
        pip_args=("--index-url", "https://example.invalid/simple"),
        location="https://example.invalid/simple",
    )

    command = installer.build_cuda_runtime_install_command(request, source, python_executable="python.exe")

    assert command == [
        "python.exe",
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--disable-pip-version-check",
        "--no-warn-script-location",
        "--target",
        "C:/runtime",
        "--index-url",
        "https://example.invalid/simple",
        "nvidia-cublas-cu12",
        "nvidia-cudnn-cu12",
    ]


class _FakePipe:
    def __init__(self, lines=()):
        self._lines = list(lines)
        self.closed = False

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        self.closed = True


class _FakeProcess:
    def __init__(self, *, returncode=0, stdout=(), stderr=()):
        self.returncode = returncode
        self.stdout = _FakePipe(stdout)
        self.stderr = _FakePipe(stderr)
        self.terminated = False
        self.killed = False
        self._poll_calls = 0

    def poll(self):
        self._poll_calls += 1
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return self.returncode


def test_cuda_runtime_installer_nonzero_exit_includes_stderr_diagnostics(monkeypatch):
    process = _FakeProcess(returncode=7, stderr=["first error\n", "second error\n"])
    monkeypatch.setattr(installer.subprocess, "Popen", lambda *_args, **_kwargs: process)

    reporter = SimpleNamespace(emit=lambda *_args, **_kwargs: None)
    diagnostics = installer.BoundedLineBuffer(max_lines=20)

    with pytest.raises(RuntimeError) as exc_info:
        installer._run_install_command(
            install_command=["python", "-m", "pip"],
            reporter=reporter,
            diagnostics=diagnostics,
            cancel_event=threading.Event(),
        )

    message = str(exc_info.value)
    assert "exit code 7" in message
    assert "first error" in message
    assert "second error" in message


def test_cuda_runtime_installer_cancel_event_terminates_process(monkeypatch):
    class _RunningProcess(_FakeProcess):
        def __init__(self):
            super().__init__(returncode=None)

        def poll(self):
            return None

        def wait(self, timeout=None):
            self.returncode = -15
            return self.returncode

    process = _RunningProcess()
    cancel_event = threading.Event()
    monkeypatch.setattr(installer.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(installer.time, "sleep", lambda _seconds: cancel_event.set())

    with pytest.raises(installer.CudaRuntimeInstallCanceledError):
        installer._run_install_command(
            install_command=["python", "-m", "pip"],
            reporter=SimpleNamespace(emit=lambda *_args, **_kwargs: None),
            diagnostics=installer.BoundedLineBuffer(max_lines=20),
            cancel_event=cancel_event,
        )

    assert process.terminated is True


def test_runtime_helper_main_invalid_stdin_emits_failed_event(monkeypatch):
    stdout = StringIO()
    monkeypatch.setattr(helper_main.sys, "stdin", StringIO(""))
    monkeypatch.setattr(helper_main.sys, "stdout", stdout)
    monkeypatch.setattr(helper_main, "_install_subtitle_signal_handlers", lambda *_args, **_kwargs: None)

    assert helper_main.run_subtitle_generation_helper() == 1

    events = _json_events(stdout)
    assert events[-1]["event"] == EVENT_FAILED
    assert "payload is missing" in events[-1]["user_message"]


def test_runtime_helper_main_success_failed_and_canceled_events(monkeypatch, workspace_tmp_path):
    request = SubtitleGenerationRequest(
        media_path="movie.mkv",
        audio_stream_index=2,
        audio_language="en",
        device="cpu",
        model_size="small",
        output_format="srt",
        output_path=str(workspace_tmp_path / "movie.srt"),
        auto_open_after_generation=True,
    )

    class _SuccessMaker:
        def __init__(self, model_size, device):
            self.model_size = model_size
            self.device = device
            self.cancel_calls = 0

        def transcribe_file(self, media_path, **kwargs):
            assert media_path == "movie.mkv"
            assert kwargs["audio_stream_index"] == 2
            assert kwargs["language"] == "en"
            kwargs["progress_callback"]("Working", 50, "details")
            return [SubtitleSegment(0, 1, "ok")]

        def save_subtitles(self, segments, output_path, output_format, **kwargs):
            assert segments == [SubtitleSegment(0, 1, "ok")]
            assert output_format == "srt"
            return output_path

        def cancel(self):
            self.cancel_calls += 1

    monkeypatch.setattr(helper_main, "_install_subtitle_signal_handlers", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(helper_main, "SubtitleMaker", _SuccessMaker)
    stdout = StringIO()
    monkeypatch.setattr(helper_main.sys, "stdin", StringIO(request.to_json()))
    monkeypatch.setattr(helper_main.sys, "stdout", stdout)

    assert helper_main.run_subtitle_generation_helper() == 0
    events = _json_events(stdout)
    assert events[0]["event"] == "progress"
    assert events[-1]["event"] == EVENT_FINISHED
    assert events[-1]["output_path"] == request.output_path

    class _FailedMaker(_SuccessMaker):
        def transcribe_file(self, *_args, **_kwargs):
            raise RuntimeError("transcribe failed")

    stdout = StringIO()
    monkeypatch.setattr(helper_main, "SubtitleMaker", _FailedMaker)
    monkeypatch.setattr(helper_main.sys, "stdin", StringIO(request.to_json()))
    monkeypatch.setattr(helper_main.sys, "stdout", stdout)

    assert helper_main.run_subtitle_generation_helper() == 1
    assert _json_events(stdout)[-1]["event"] == EVENT_FAILED
    assert "transcribe failed" in _json_events(stdout)[-1]["user_message"]

    class _CanceledMaker(_SuccessMaker):
        def transcribe_file(self, *_args, **_kwargs):
            raise SubtitleGenerationCanceledError()

    stdout = StringIO()
    monkeypatch.setattr(helper_main, "SubtitleMaker", _CanceledMaker)
    monkeypatch.setattr(helper_main.sys, "stdin", StringIO(request.to_json()))
    monkeypatch.setattr(helper_main.sys, "stdout", stdout)

    assert helper_main.run_subtitle_generation_helper() == 2
    assert _json_events(stdout)[-1]["event"] == EVENT_CANCELED


def test_runtime_installer_main_invalid_stdin_emits_failed_event(monkeypatch):
    stdout = StringIO()
    monkeypatch.setattr(installer_main.sys, "stdin", StringIO(""))
    monkeypatch.setattr(installer_main.sys, "stdout", stdout)
    monkeypatch.setattr(installer_main, "_install_signal_handlers", lambda *_args, **_kwargs: None)

    assert installer_main.run_cuda_runtime_installer() == 1

    events = _json_events(stdout)
    assert events[-1]["event"] == EVENT_FAILED
    assert "payload is missing" in events[-1]["diagnostics"]


def test_runtime_installer_main_success_failed_and_canceled_events(monkeypatch, workspace_tmp_path):
    request = CudaRuntimeInstallRequest(
        packages=("nvidia-runtime",),
        install_target=str(workspace_tmp_path / "runtime"),
    )
    monkeypatch.setattr(installer_main, "_install_signal_handlers", lambda *_args, **_kwargs: None)

    def succeed(request, emit_event, cancel_event):
        emit_event({"event": EVENT_STATUS, "status": "Installing", "details": request.install_target})
        emit_event({"event": EVENT_FINISHED})

    stdout = StringIO()
    monkeypatch.setattr(installer_main, "ensure_cuda_runtime_installed", succeed)
    monkeypatch.setattr(installer_main.sys, "stdin", StringIO(request.to_json()))
    monkeypatch.setattr(installer_main.sys, "stdout", stdout)

    assert installer_main.run_cuda_runtime_installer() == 0
    events = _json_events(stdout)
    assert events[0]["event"] == EVENT_STATUS
    assert events[-1]["event"] == EVENT_FINISHED

    def fail(*_args, **_kwargs):
        raise RuntimeError("install failed")

    stdout = StringIO()
    monkeypatch.setattr(installer_main, "ensure_cuda_runtime_installed", fail)
    monkeypatch.setattr(installer_main.sys, "stdin", StringIO(request.to_json()))
    monkeypatch.setattr(installer_main.sys, "stdout", stdout)

    assert installer_main.run_cuda_runtime_installer() == 1
    failed = _json_events(stdout)[-1]
    assert failed["event"] == EVENT_FAILED
    assert "install failed" in failed["diagnostics"]

    def cancel(*_args, **_kwargs):
        raise installer_main.CudaRuntimeInstallCanceledError("stop")

    stdout = StringIO()
    monkeypatch.setattr(installer_main, "ensure_cuda_runtime_installed", cancel)
    monkeypatch.setattr(installer_main.sys, "stdin", StringIO(request.to_json()))
    monkeypatch.setattr(installer_main.sys, "stdout", stdout)

    assert installer_main.run_cuda_runtime_installer() == 2
    assert _json_events(stdout)[-1]["event"] == EVENT_CANCELED
