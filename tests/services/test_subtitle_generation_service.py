import importlib.util
import subprocess
import sys
from subprocess import CompletedProcess
from dataclasses import dataclass
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import QWidget

from services.MediaLibraryService import MediaLibraryService
from services.subtitles.SubtitleGenerationOutcomeHandler import SubtitleAutoOpenOutcome
from services.subtitles.SubtitleGenerationService import (
    SubtitleGenerationContext,
    SubtitlePipelineTask,
    SubtitleGenerationService,
    SubtitleGenerationState,
)
from services.subtitles.SubtitleGenerationPreflight import AudioStreamProbeState
from ui.SubtitleGenerationDialog import SubtitleGenerationDialogResult

from tests.fakes import FakePlayerWindow, FakeSubtitleWorker, FakeMediaStore


@dataclass(frozen=True)
class _AudioStream:
    stream_index: int
    label: str


class _RunningThread:
    def isRunning(self):
        return True


def _load_real_module(module_name: str, relative_path: str):
    project_root = Path(__file__).resolve().parents[2]
    module_path = project_root / relative_path
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _options(output_path: str = "C:/tmp/subtitles.srt") -> SubtitleGenerationDialogResult:
    return SubtitleGenerationDialogResult(
        audio_stream_index=None,
        audio_language=None,
        device=None,
        model_size="small",
        output_format="srt",
        output_path=output_path,
        auto_open_after_generation=True,
    )


def _make_service(parent: QWidget, player: FakePlayerWindow, store: FakeMediaStore | None = None) -> tuple[SubtitleGenerationService, FakeMediaStore]:
    resolved_store = store or FakeMediaStore()
    media_library = MediaLibraryService(parent, player, resolved_store)
    return SubtitleGenerationService(parent, player, resolved_store, media_library), resolved_store


def _seed_active_run(service: SubtitleGenerationService, media_path: str = "C:/media/movie.mkv", request_id: int = 7):
    service._state = SubtitleGenerationState.RUNNING
    service._active_run = service._begin_pipeline_run(
        SubtitleGenerationContext(media_path=media_path, request_id=request_id),
        _options(),
    )
    return service._active_run


def test_generation_starts_from_idle_and_rejects_reentry(monkeypatch):
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/movie.mkv"
    player.playback._request_id = 7
    parent = QWidget()
    service, _store = _make_service(parent, player)

    launches = []
    already_running_calls = []

    def fake_launch(run, options):
        launches.append((run, options))
        service._state = SubtitleGenerationState.RUNNING

    monkeypatch.setattr(service, "_launch_subtitle_generation", fake_launch)
    monkeypatch.setattr(
        "services.subtitles.SubtitleGenerationService.show_subtitle_generation_already_running",
        lambda _parent: already_running_calls.append(True),
    )

    assert service.generate_subtitle() is True
    assert service._state == SubtitleGenerationState.DIALOG_OPEN
    assert service._ui.dialog_requests[-1]["media_path"] == "C:/media/movie.mkv"

    probe_request_id = service._current_audio_stream_probe_request_id
    service._on_audio_stream_probe_finished(
        probe_request_id,
        "C:/media/movie.mkv",
        [_AudioStream(1, "Audio 1")],
    )

    service._ui.dialog_requests[-1]["on_generate"](_options())

    assert len(launches) == 1
    assert launches[0][0].context.media_path == "C:/media/movie.mkv"
    # This test stubs the real launch path, so deferred UI suspend is not expected here.
    assert player.suspend_calls == 0
    assert service._active_run is not None

    assert service.generate_subtitle() is False
    assert already_running_calls == [True]
    assert service._ui.focus_calls == 1


def test_generation_dialog_pause_respects_existing_playback_interruption():
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/movie.mkv"
    player.playback._request_id = 7
    player.playback._is_playing = True
    player.playback.pause_for_interruption("pip_rebind")
    player.playback.pause()
    service, _store = _make_service(QWidget(), player)

    assert service.generate_subtitle() is True
    service._ui.dialog_requests[-1]["on_cancel"]()

    assert player.playback.pause_calls == 1
    assert player.playback.play_calls == 0
    assert player.playback.is_playing() is False


def test_cancel_transitions_to_canceling_and_is_idempotent():
    player = FakePlayerWindow()
    service, _store = _make_service(QWidget(), player)
    worker = FakeSubtitleWorker()

    _seed_active_run(service)
    service._subtitle_worker = worker

    service._request_active_task_stop()
    service._request_active_task_stop()

    assert service._state == SubtitleGenerationState.CANCELING
    assert worker.cancel_calls == 1
    assert service._ui.cancel_pending_calls == 1


def test_cancel_active_cuda_install_uses_unified_stop_path():
    player = FakePlayerWindow()
    service, _store = _make_service(QWidget(), player)

    run = _seed_active_run(service)
    run.task = SubtitlePipelineTask.CUDA_INSTALL
    service._cuda_runtime_flow._active = True

    service._request_active_task_stop()
    service._request_active_task_stop()

    assert service._state == SubtitleGenerationState.CANCELING
    assert service._cuda_runtime_flow.request_stop_calls == [False]
    assert service._ui.cuda_cancel_pending_calls == 1


def test_begin_shutdown_requests_graceful_stop_for_active_worker():
    player = FakePlayerWindow()
    service, _store = _make_service(QWidget(), player)
    worker = FakeSubtitleWorker()

    _seed_active_run(service)
    service._subtitle_worker = worker
    service._subtitle_thread = _RunningThread()

    pending = service.begin_shutdown()

    assert pending is True
    assert service._state == SubtitleGenerationState.SHUTTING_DOWN
    assert worker.cancel_calls == 1
    assert service._ui.closed_generation_dialogs == 1
    assert service.is_shutdown_in_progress() is True


def test_begin_shutdown_requests_graceful_stop_for_active_cuda_flow():
    player = FakePlayerWindow()
    service, _store = _make_service(QWidget(), player)

    run = _seed_active_run(service)
    run.task = SubtitlePipelineTask.CUDA_INSTALL
    service._cuda_runtime_flow._active = True

    pending = service.begin_shutdown()

    assert pending is True
    assert service._state == SubtitleGenerationState.SHUTTING_DOWN
    assert service._cuda_runtime_flow.request_stop_calls == [False]
    assert service._ui.closed_generation_dialogs == 1
    assert service.is_shutdown_in_progress() is True


def test_begin_force_shutdown_requests_force_stop_for_active_worker():
    player = FakePlayerWindow()
    service, _store = _make_service(QWidget(), player)
    worker = FakeSubtitleWorker()

    _seed_active_run(service)
    service._subtitle_worker = worker
    service._subtitle_thread = _RunningThread()

    pending = service.begin_force_shutdown()

    assert pending is True
    assert service._state == SubtitleGenerationState.SHUTTING_DOWN
    assert worker.force_stop_calls == 1
    assert service._ui.closed_progress_dialogs == 1


def test_begin_force_shutdown_requests_force_stop_for_active_cuda_flow():
    player = FakePlayerWindow()
    service, _store = _make_service(QWidget(), player)

    run = _seed_active_run(service)
    run.task = SubtitlePipelineTask.CUDA_INSTALL
    service._cuda_runtime_flow._active = True

    pending = service.begin_force_shutdown()

    assert pending is True
    assert service._state == SubtitleGenerationState.SHUTTING_DOWN
    assert service._cuda_runtime_flow.request_stop_calls == [True]
    assert service._ui.closed_progress_dialogs == 1


def test_stale_run_events_are_ignored():
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/movie.mkv"
    player.playback._request_id = 7
    service, _store = _make_service(QWidget(), player)

    current_run = _seed_active_run(service)

    service._on_worker_progress_changed(current_run.run_id + 1, 42)
    service._on_subtitle_generation_finished(current_run.run_id + 1, "C:/tmp/out.srt", True, False)

    assert service._ui.progress_updates == []
    assert service._active_run is current_run
    assert player.resume_calls == 0


def test_terminal_completion_clears_active_run_and_resumes_player_ui():
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/movie.mkv"
    player.playback._request_id = 7
    store = FakeMediaStore()
    service, _store = _make_service(QWidget(), player, store)

    run = _seed_active_run(service)
    service._playback_takeover.acquire()
    service._playback_takeover.suspend_player_ui()

    service._on_subtitle_generation_finished(run.run_id, "C:/tmp/generated.srt", True, False)

    assert service._state == SubtitleGenerationState.SUCCEEDED
    assert service._active_run is None
    assert player.resume_calls == 1
    assert player.playback.opened_subtitles == ["C:/tmp/generated.srt"]
    assert store.saved_last_open_dir == ["C:/tmp/generated.srt"]
    assert service._outcomes.successes


def test_generation_dialog_cancel_releases_takeover_atomically():
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/movie.mkv"
    player.playback._request_id = 7
    player.playback._is_playing = True
    service, _store = _make_service(QWidget(), player)

    assert service.generate_subtitle() is True
    service._ui.dialog_requests[-1]["on_cancel"]()

    assert service._state == SubtitleGenerationState.IDLE
    assert player.playback.pause_calls == 1
    assert player.playback.play_calls == 1
    assert player.resume_calls == 0
    assert player.playback.interruptions == {}


def test_shutdown_clears_takeover_without_resuming_playback():
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/movie.mkv"
    player.playback._request_id = 7
    player.playback._is_playing = True
    service, _store = _make_service(QWidget(), player)

    assert service.generate_subtitle() is True
    pending = service.begin_shutdown()

    assert pending is False
    assert player.playback.pause_calls == 1
    assert player.playback.play_calls == 0
    assert player.playback.interruptions == {}


def test_generated_auto_open_uses_unified_context_guard():
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/other.mkv"
    player.playback._request_id = 99
    store = FakeMediaStore()
    service, _store = _make_service(QWidget(), player, store)

    run = _seed_active_run(service, media_path="C:/media/movie.mkv", request_id=7)

    service._on_subtitle_generation_finished(run.run_id, "C:/tmp/generated.srt", True, False)

    assert player.playback.opened_subtitles == []
    assert store.saved_last_open_dir == ["C:/tmp/generated.srt"]
    assert service._outcomes.successes[-1][1] == SubtitleAutoOpenOutcome.CONTEXT_CHANGED


def test_generated_auto_open_uses_unified_failure_path():
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/movie.mkv"
    player.playback._request_id = 7
    player.playback.open_subtitle_result = False
    store = FakeMediaStore()
    service, _store = _make_service(QWidget(), player, store)

    run = _seed_active_run(service)

    service._on_subtitle_generation_finished(run.run_id, "C:/tmp/generated.srt", True, False)

    assert player.playback.opened_subtitles == ["C:/tmp/generated.srt"]
    assert store.saved_last_open_dir == ["C:/tmp/generated.srt"]
    assert service._outcomes.successes[-1][1] == SubtitleAutoOpenOutcome.LOAD_FAILED


def test_generate_stays_non_blocking_while_audio_tracks_are_loading(monkeypatch):
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/movie.mkv"
    player.playback._request_id = 7
    service, _store = _make_service(QWidget(), player)

    loading_messages = []
    launch_calls = []
    message_box = sys.modules["ui.MessageBoxService"]

    monkeypatch.setattr(message_box, "show_audio_streams_still_loading", lambda _parent: loading_messages.append(True))
    monkeypatch.setattr(
        service,
        "_launch_subtitle_generation",
        lambda run, options: launch_calls.append((run, options)),
    )

    assert service.generate_subtitle() is True
    assert service._audio_stream_probe_state == AudioStreamProbeState.LOADING
    assert service._ui.audio_tracks_loading_calls == 1
    assert len(service._audio_stream_probe_workers) == 1

    service._ui.dialog_requests[-1]["on_generate"](_options())

    assert loading_messages == [True]
    assert launch_calls == []
    assert service._state == SubtitleGenerationState.DIALOG_OPEN
    assert service._active_run is None


def test_generate_starts_normally_after_audio_probe_ready(monkeypatch):
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/movie.mkv"
    player.playback._request_id = 7
    service, _store = _make_service(QWidget(), player)

    launches = []
    service.generate_subtitle()

    probe_request_id = service._current_audio_stream_probe_request_id
    service._on_audio_stream_probe_finished(
        probe_request_id,
        player.playback._media_path,
        [_AudioStream(1, "Audio 1"), _AudioStream(2, "Audio 2")],
    )

    monkeypatch.setattr(
        service,
        "_launch_subtitle_generation",
        lambda run, options: launches.append((run, options)),
    )

    service._ui.dialog_requests[-1]["on_generate"](_options())

    assert len(launches) == 1
    assert launches[0][0].context.media_path == player.playback._media_path
    assert service._ui.applied_audio_tracks[-1]["audio_tracks"] == [
        (None, "Current / default"),
        (1, "Audio 1"),
        (2, "Audio 2"),
    ]


def test_generate_reuses_cached_audio_probe_failure_without_sync_probe(monkeypatch):
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/movie.mkv"
    player.playback._request_id = 7
    service, _store = _make_service(QWidget(), player)

    failed_messages = []
    launch_calls = []
    message_box = sys.modules["ui.MessageBoxService"]

    monkeypatch.setattr(message_box, "show_audio_stream_inspection_failed", lambda _parent, reason: failed_messages.append(reason))
    monkeypatch.setattr(
        service,
        "_launch_subtitle_generation",
        lambda run, options: launch_calls.append((run, options)),
    )

    service.generate_subtitle()
    probe_request_id = service._current_audio_stream_probe_request_id
    service._on_audio_stream_probe_failed(probe_request_id, player.playback._media_path, "probe failed")

    service._ui.dialog_requests[-1]["on_generate"](_options())

    assert failed_messages == ["probe failed"]
    assert launch_calls == []
    assert service._state == SubtitleGenerationState.DIALOG_OPEN
    assert service._active_run is None


def test_stale_audio_probe_result_is_ignored_after_dialog_close():
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/movie.mkv"
    service, _store = _make_service(QWidget(), player)

    service.generate_subtitle()
    probe_request_id = service._current_audio_stream_probe_request_id
    service._ui.dialog_requests[-1]["on_cancel"]()

    service._on_audio_stream_probe_finished(
        probe_request_id,
        player.playback._media_path,
        [_AudioStream(3, "Late track")],
    )

    assert service._ui.applied_audio_tracks == []
    assert service._audio_stream_probe_state == AudioStreamProbeState.IDLE
    assert service._state == SubtitleGenerationState.IDLE


def test_stale_audio_probe_result_is_ignored_after_dialog_reopen():
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/movie.mkv"
    service, _store = _make_service(QWidget(), player)

    service.generate_subtitle()
    first_probe_request_id = service._current_audio_stream_probe_request_id
    service._ui.dialog_requests[-1]["on_cancel"]()

    service.generate_subtitle()
    second_probe_request_id = service._current_audio_stream_probe_request_id

    service._on_audio_stream_probe_finished(
        first_probe_request_id,
        player.playback._media_path,
        [_AudioStream(1, "Old track")],
    )
    service._on_audio_stream_probe_finished(
        second_probe_request_id,
        player.playback._media_path,
        [_AudioStream(2, "Fresh track")],
    )

    assert service._ui.applied_audio_tracks == [
        {
            "audio_tracks": [(None, "Current / default"), (2, "Fresh track")],
            "selected_track_id": None,
            "selector_enabled": True,
            "generate_enabled": True,
        }
    ]


def test_real_preflight_validation_never_falls_back_to_sync_probe(monkeypatch):
    module = _load_real_module(
        "real_subtitle_generation_preflight_test",
        "services/subtitles/SubtitleGenerationPreflight.py",
    )

    preflight = module.SubtitleGenerationPreflight(QWidget())
    options = _options()
    loading_messages = []
    failed_messages = []

    monkeypatch.setattr(module, "show_audio_streams_still_loading", lambda _parent: loading_messages.append(True))
    monkeypatch.setattr(module, "show_audio_stream_inspection_failed", lambda _parent, reason: failed_messages.append(reason))
    monkeypatch.setattr(module, "show_no_audio_streams_found", lambda _parent: None)
    monkeypatch.setattr(preflight, "_validate_output_path", lambda _options: True)

    result = preflight.validate_generation_request(
        "C:/media/movie.mkv",
        options,
        probe_state=module.AudioStreamProbeState.IDLE,
    )
    assert result.is_valid is False
    assert loading_messages == [True]

    result = preflight.validate_generation_request(
        "C:/media/movie.mkv",
        options,
        probe_state=module.AudioStreamProbeState.LOADING,
    )
    assert result.is_valid is False
    assert loading_messages == [True, True]

    result = preflight.validate_generation_request(
        "C:/media/movie.mkv",
        options,
        probe_state=module.AudioStreamProbeState.FAILED,
        probe_error="cached failure",
    )
    assert result.is_valid is False
    assert failed_messages == ["cached failure"]


def test_real_audio_stream_probe_worker_emits_finished_with_list_result(monkeypatch):
    module = _load_real_module(
        "real_subtitle_generation_workers_test",
        "services/subtitles/SubtitleGenerationWorkers.py",
    )

    monkeypatch.setattr(module, "probe_audio_streams", lambda media_path: (_AudioStream(1, f"{media_path}-track"),))

    worker = module.AudioStreamProbeWorker(11, "C:/media/movie.mkv")
    finished_calls = []
    failed_calls = []
    worker.finished.connect(lambda request_id, media_path, audio_streams: finished_calls.append((request_id, media_path, audio_streams)))
    worker.failed.connect(lambda request_id, media_path, reason: failed_calls.append((request_id, media_path, reason)))

    worker._run()

    assert finished_calls == [(11, "C:/media/movie.mkv", [_AudioStream(1, "C:/media/movie.mkv-track")])]
    assert failed_calls == []


def test_real_audio_stream_probe_worker_emits_failure_on_probe_error(monkeypatch):
    module = _load_real_module(
        "real_subtitle_generation_workers_failure_test",
        "services/subtitles/SubtitleGenerationWorkers.py",
    )

    monkeypatch.setattr(module, "probe_audio_streams", lambda _media_path: (_ for _ in ()).throw(RuntimeError("probe boom")))

    worker = module.AudioStreamProbeWorker(12, "C:/media/broken.mkv")
    finished_calls = []
    failed_calls = []
    worker.finished.connect(lambda request_id, media_path, audio_streams: finished_calls.append((request_id, media_path, audio_streams)))
    worker.failed.connect(lambda request_id, media_path, reason: failed_calls.append((request_id, media_path, reason)))

    worker._run()

    assert finished_calls == []
    assert failed_calls == [(12, "C:/media/broken.mkv", "probe boom")]


def test_real_audio_stream_probe_worker_start_is_idempotent(monkeypatch):
    module = _load_real_module(
        "real_subtitle_generation_workers_start_test",
        "services/subtitles/SubtitleGenerationWorkers.py",
    )

    created_threads = []

    class _FakeThread:
        def __init__(self, *, target, name, daemon):
            self.target = target
            self.name = name
            self.daemon = daemon
            self.start_calls = 0
            created_threads.append(self)

        def start(self):
            self.start_calls += 1

    monkeypatch.setattr(module.threading, "Thread", _FakeThread)

    worker = module.AudioStreamProbeWorker(13, "C:/media/movie.mkv")
    worker.start()
    worker.start()

    assert len(created_threads) == 1
    assert created_threads[0].name == "audio-stream-probe-13"
    assert created_threads[0].daemon is True
    assert created_threads[0].start_calls == 1


def test_real_probe_audio_streams_reports_timeout(monkeypatch):
    module = _load_real_module(
        "real_subtitle_maker_timeout_test",
        "services/subtitles/SubtitleMaker.py",
    )

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd=["ffprobe"], timeout=module.FFPROBE_AUDIO_STREAM_TIMEOUT_SECONDS)),
    )

    with pytest.raises(RuntimeError, match="timed out after"):
        module.probe_audio_streams("C:/media/movie.mkv")


def test_real_probe_audio_streams_reports_missing_ffprobe(monkeypatch):
    module = _load_real_module(
        "real_subtitle_maker_missing_test",
        "services/subtitles/SubtitleMaker.py",
    )

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("ffprobe")),
    )

    with pytest.raises(RuntimeError, match="ffprobe was not found"):
        module.probe_audio_streams("C:/media/movie.mkv")


def test_real_probe_audio_streams_reports_non_zero_exit(monkeypatch):
    module = _load_real_module(
        "real_subtitle_maker_exitcode_test",
        "services/subtitles/SubtitleMaker.py",
    )

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(args=["ffprobe"], returncode=2, stdout="", stderr="ffprobe failed"),
    )

    with pytest.raises(RuntimeError, match="Failed to inspect audio streams: ffprobe failed"):
        module.probe_audio_streams("C:/media/movie.mkv")


def test_real_probe_audio_streams_reports_invalid_json(monkeypatch):
    module = _load_real_module(
        "real_subtitle_maker_badjson_test",
        "services/subtitles/SubtitleMaker.py",
    )

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(args=["ffprobe"], returncode=0, stdout="not-json", stderr=""),
    )

    with pytest.raises(RuntimeError, match="invalid audio stream metadata"):
        module.probe_audio_streams("C:/media/movie.mkv")


def test_real_probe_audio_streams_reports_unexpected_payload(monkeypatch):
    module = _load_real_module(
        "real_subtitle_maker_payload_test",
        "services/subtitles/SubtitleMaker.py",
    )

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(args=["ffprobe"], returncode=0, stdout='["unexpected"]', stderr=""),
    )

    with pytest.raises(RuntimeError, match="unexpected audio stream response"):
        module.probe_audio_streams("C:/media/movie.mkv")


def test_real_probe_audio_streams_reports_malformed_streams_payload(monkeypatch):
    module = _load_real_module(
        "real_subtitle_maker_malformed_test",
        "services/subtitles/SubtitleMaker.py",
    )

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(args=["ffprobe"], returncode=0, stdout='{"streams": {"index": 1}}', stderr=""),
    )

    with pytest.raises(RuntimeError, match="malformed audio stream metadata"):
        module.probe_audio_streams("C:/media/movie.mkv")


def test_real_generation_dialog_opens_immediately_in_loading_state_and_updates_tracks():
    dialog_module = _load_real_module(
        "real_subtitle_generation_dialog_test",
        "ui/SubtitleGenerationDialog.py",
    )
    coordinator_module = _load_real_module(
        "real_subtitle_generation_ui_coordinator_test",
        "services/subtitles/SubtitleGenerationUiCoordinator.py",
    )

    app = QApplication.instance() or QApplication([])
    parent = QWidget()
    coordinator = coordinator_module.SubtitleGenerationUiCoordinator(
        parent,
        theme_color_getter=lambda: __import__("models.ThemeColor", fromlist=["ThemeState"]).ThemeState(),
    )

    generated = []
    canceled = []
    coordinator.open_generation_dialog(
        "C:/media/movie.mkv",
        on_generate=lambda result: generated.append(result),
        on_cancel=lambda: canceled.append(True),
    )
    app.processEvents()

    dialog = coordinator._generation_dialog
    assert dialog is not None
    assert dialog.isVisible() is True
    assert dialog.generate_button.isEnabled() is False
    assert dialog.audio_track_combo.isEnabled() is False
    assert dialog.audio_track_combo.itemText(0) == dialog_module.SubtitleGenerationDialog.AUDIO_TRACKS_LOADING_LABEL

    coordinator.apply_generation_dialog_audio_tracks(
        [(None, "Current / default"), (2, "Audio 2")],
        selected_track_id=2,
        selector_enabled=True,
        generate_enabled=True,
    )
    app.processEvents()

    assert dialog.generate_button.isEnabled() is True
    assert dialog.audio_track_combo.isEnabled() is True
    assert dialog.audio_track_combo.count() == 2
    assert dialog.audio_track_combo.itemText(1) == "Audio 2"
    assert dialog.audio_track_combo.currentData() == 2

    dialog.close_button.click()
    app.processEvents()
    assert canceled == [True]


def test_real_audio_probe_worker_start_result_is_ignored_after_fast_reopen(monkeypatch):
    workers_module = _load_real_module(
        "real_subtitle_generation_workers_reopen_test",
        "services/subtitles/SubtitleGenerationWorkers.py",
    )
    service_module = sys.modules["services.subtitles.SubtitleGenerationService"]

    scheduled_targets = []

    class _DeferredThread:
        def __init__(self, *, target, name, daemon):
            self._target = target
            self.name = name
            self.daemon = daemon
            self.start_calls = 0

        def start(self):
            self.start_calls += 1
            scheduled_targets.append(self._target)

    monkeypatch.setattr(workers_module.threading, "Thread", _DeferredThread)
    monkeypatch.setattr(
        workers_module,
        "probe_audio_streams",
        lambda media_path: [_AudioStream(1, f"{media_path}-track-{len(scheduled_targets)}")],
    )
    monkeypatch.setattr(service_module, "AudioStreamProbeWorker", workers_module.AudioStreamProbeWorker)

    app = QApplication.instance() or QApplication([])
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/movie.mkv"
    player.playback._request_id = 7
    service, _store = _make_service(QWidget(), player)

    assert service.generate_subtitle() is True
    first_probe_request_id = service._current_audio_stream_probe_request_id
    assert len(scheduled_targets) == 1

    service._ui.dialog_requests[-1]["on_cancel"]()
    assert service.generate_subtitle() is True
    second_probe_request_id = service._current_audio_stream_probe_request_id
    assert second_probe_request_id != first_probe_request_id
    assert len(scheduled_targets) == 2

    scheduled_targets[0]()
    app.processEvents()
    assert service._ui.applied_audio_tracks == []
    assert service._current_audio_stream_probe_request_id == second_probe_request_id

    scheduled_targets[1]()
    app.processEvents()
    assert service._ui.applied_audio_tracks == [
        {
            "audio_tracks": [(None, "Current / default"), (1, "C:/media/movie.mkv-track-2")],
            "selected_track_id": None,
            "selector_enabled": True,
            "generate_enabled": True,
        }
    ]


def test_real_pip_window_left_edge_anchor_matches_windows_fixed_aspect_behavior():
    module = _load_real_module(
        "real_pip_window_anchor_test",
        "ui/PiPWindow.py",
    )

    pip_window = module.PiPWindow.__new__(module.PiPWindow)
    pip_window._active_edges = pip_window._EDGE_LEFT

    anchored_left, anchored_top = module.PiPWindow._anchored_position(
        pip_window,
        target_width=320,
        target_height=180,
        press_left=100,
        press_top=100,
        press_right=500,
        press_bottom=400,
    )

    assert (anchored_left, anchored_top) == (180, 220)


def test_real_output_path_preflight_does_not_mutate_filesystem(workspace_tmp_path):
    module = _load_real_module(
        "real_subtitle_generation_preflight_fs_test",
        "services/subtitles/SubtitleGenerationPreflight.py",
    )

    preflight = module.SubtitleGenerationPreflight(QWidget())
    output_path = workspace_tmp_path / "nested" / "deeper" / "movie.srt"

    assert output_path.parent.exists() is False

    result = preflight._preflight_subtitle_output_path(output_path)

    assert result is None
    assert output_path.parent.exists() is False
    assert list(workspace_tmp_path.rglob("*")) == []


def test_real_subtitle_save_creates_directory_only_during_actual_write(workspace_tmp_path):
    module = _load_real_module(
        "real_subtitle_maker_save_dir_test",
        "services/subtitles/SubtitleMaker.py",
    )

    maker = module.SubtitleMaker()
    output_path = workspace_tmp_path / "nested" / "movie.srt"
    segments = [module.SubtitleSegment(start=0.0, end=1.5, text="Hello")]

    assert output_path.parent.exists() is False

    saved_output_path = maker.save_srt(segments, str(output_path))

    assert output_path.parent.exists() is True
    assert Path(saved_output_path) == output_path
    assert output_path.read_text(encoding="utf-8").startswith("1\n00:00:00,000 --> 00:00:01,500\nHello")


def test_real_subtitle_save_keeps_fallback_output_path_behavior(monkeypatch, workspace_tmp_path):
    module = _load_real_module(
        "real_subtitle_maker_fallback_save_test",
        "services/subtitles/SubtitleMaker.py",
    )

    maker = module.SubtitleMaker()
    output_path = workspace_tmp_path / "movie.srt"
    output_path.write_text("existing", encoding="utf-8")
    segments = [module.SubtitleSegment(start=0.0, end=1.0, text="Hello")]
    original_replace = module.os.replace
    replace_calls = []

    def fake_replace(src, dst):
        replace_calls.append((Path(src).name, Path(dst).name))
        if Path(dst) == output_path:
            raise PermissionError("destination is in use")
        return original_replace(src, dst)

    monkeypatch.setattr(module.os, "replace", fake_replace)

    saved_output_path = maker.save_srt(segments, str(output_path))

    assert Path(saved_output_path) == workspace_tmp_path / "movie (1).srt"
    assert Path(saved_output_path).exists() is True
    assert output_path.read_text(encoding="utf-8") == "existing"
    assert replace_calls[0][1] == "movie.srt"
    assert replace_calls[1][1] == "movie (1).srt"
