import importlib.util
import subprocess
import sys
from subprocess import CompletedProcess
from dataclasses import dataclass
from pathlib import Path

import pytest
from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import QWidget

from services.MediaLibraryService import MediaLibraryService
from services.subtitles.SubtitleGenerationService import (
    SubtitleGenerationContext,
    SubtitlePipelineTask,
    SubtitleGenerationService,
    SubtitleServiceState,
    SubtitlePipelinePhase,
    SubtitlePipelineResult,
)
from services.subtitles.SubtitleGenerationPreflight import (
    AudioStreamProbeState,
    SubtitleGenerationValidationFailure,
    SubtitleGenerationValidationResult,
)
from models import SubtitleGenerationDialogResult

from tests.fakes import FakePlayerWindow, FakeSubtitleWorker, FakeMediaStore


@dataclass(frozen=True)
class _AudioStream:
    stream_index: int
    label: str


class _RunningThread:
    def isRunning(self):
        return True


class _ProbeThread:
    def __init__(self):
        self.running = True

    def isRunning(self):
        return self.running

    def deleteLater(self):
        return None


class _ProbeWorker:
    def __init__(self):
        self.cancel_calls = 0
        self.force_stop_calls = 0

    def cancel(self):
        self.cancel_calls += 1

    def force_stop(self):
        self.force_stop_calls += 1

    def deleteLater(self):
        return None


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
    service._active_run = service._begin_pipeline_run(
        SubtitleGenerationContext(media_path=media_path, request_id=request_id),
        _options(),
    )
    service._active_run.phase = SubtitlePipelinePhase.RUNNING
    return service._active_run


def _seed_active_audio_probe(service: SubtitleGenerationService, probe_request_id: int = 101):
    thread = _ProbeThread()
    worker = _ProbeWorker()
    service._audio_probe_flow._begin_probe("C:/media/movie.mkv")
    service._audio_probe_flow._current_probe_request_id = probe_request_id
    service._audio_probe_flow._threads[probe_request_id] = thread
    service._audio_probe_flow._workers[probe_request_id] = worker
    return probe_request_id, thread, worker


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
        run.phase = SubtitlePipelinePhase.RUNNING

    monkeypatch.setattr(service, "_launch_subtitle_generation", fake_launch)
    monkeypatch.setattr(
        "services.subtitles.SubtitleGenerationService.show_subtitle_generation_already_running",
        lambda _parent: already_running_calls.append(True),
    )

    assert service.generate_subtitle() is True
    assert service._service_state == SubtitleServiceState.DIALOG_OPEN
    assert service._ui.dialog_requests[-1]["media_path"] == "C:/media/movie.mkv"

    probe_request_id = service._audio_probe_flow.current_probe_request_id
    service._audio_probe_flow._on_probe_finished(
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
    assert service._service_state == SubtitleServiceState.DIALOG_OPEN

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


def test_pipeline_thread_guard_matches_owner_thread():
    app_thread = object()

    assert (
        SubtitleGenerationService._is_pipeline_thread(
            is_main_thread=True,
            service_thread=app_thread,
            app_thread=app_thread,
        )
        is True
    )
    assert (
        SubtitleGenerationService._is_pipeline_thread(
            is_main_thread=False,
            service_thread=app_thread,
            app_thread=app_thread,
        )
        is False
    )
    assert (
        SubtitleGenerationService._is_pipeline_thread(
            is_main_thread=True,
            service_thread=object(),
            app_thread=app_thread,
        )
        is False
    )


def test_cancel_transitions_to_canceling_and_is_idempotent():
    player = FakePlayerWindow()
    service, _store = _make_service(QWidget(), player)
    worker = FakeSubtitleWorker()

    run = _seed_active_run(service)
    run.task = SubtitlePipelineTask.SUBTITLE_GENERATION
    run.subtitle_worker = worker

    service._request_active_task_stop()
    service._request_active_task_stop()

    assert service._service_state == SubtitleServiceState.IDLE
    assert run.phase == SubtitlePipelinePhase.CANCELING
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

    assert service._service_state == SubtitleServiceState.IDLE
    assert run.phase == SubtitlePipelinePhase.CANCELING
    assert service._cuda_runtime_flow.request_stop_calls == [False]
    assert service._ui.cuda_cancel_pending_calls == 1


def test_active_run_phase_is_pipeline_lifecycle_owner(monkeypatch):
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/movie.mkv"
    service, _store = _make_service(QWidget(), player)
    already_running_calls = []

    run = _seed_active_run(service)
    run.task = SubtitlePipelineTask.SUBTITLE_GENERATION
    service._service_state = SubtitleServiceState.IDLE

    monkeypatch.setattr(
        "services.subtitles.SubtitleGenerationService.show_subtitle_generation_already_running",
        lambda _parent: already_running_calls.append(True),
    )

    assert service.has_active_tasks() is True
    assert service._service_state == SubtitleServiceState.IDLE
    assert run.phase == SubtitlePipelinePhase.RUNNING
    assert service.generate_subtitle() is False
    assert already_running_calls == [True]

    run.phase = SubtitlePipelinePhase.SUCCEEDED

    assert service.has_active_tasks() is False


def test_cuda_install_progress_is_opened_by_service_before_flow_start(monkeypatch):
    player = FakePlayerWindow()
    service, _store = _make_service(QWidget(), player)
    service._service_state = SubtitleServiceState.DIALOG_OPEN
    run = service._begin_pipeline_run(
        SubtitleGenerationContext(media_path="C:/media/movie.mkv", request_id=7),
        _options(),
    )
    missing_packages = ["nvidia-cuda-runtime-cu12"]
    start_calls = []

    def fake_start(run_id, packages):
        start_calls.append((run_id, list(packages)))
        return True

    monkeypatch.setattr(service._cuda_runtime_flow, "start", fake_start)

    service._start_cuda_runtime_install(run, missing_packages)

    assert service._ui.progress_requests == [
        {
            "options": missing_packages,
            "on_cancel": service._request_active_task_stop,
        }
    ]
    assert start_calls == [(run.run_id, missing_packages)]
    assert run.task == SubtitlePipelineTask.CUDA_INSTALL
    assert run.phase == SubtitlePipelinePhase.RUNNING
    assert service._service_state == SubtitleServiceState.IDLE


def test_cuda_download_prompt_does_not_start_install_after_context_change(monkeypatch):
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/movie.mkv"
    player.playback._request_id = 7
    service, _store = _make_service(QWidget(), player)

    service.generate_subtitle()

    probe_request_id = service._audio_probe_flow.current_probe_request_id
    service._audio_probe_flow._on_probe_finished(
        probe_request_id,
        player.playback._media_path,
        [_AudioStream(1, "Audio 1")],
    )
    start_calls = []

    def prompt_and_change_media(_parent, _packages):
        player.playback._media_path = "C:/media/other.mkv"
        player.playback._request_id = 8
        return "download"

    monkeypatch.setattr(
        "services.subtitles.SubtitleGenerationService.get_missing_windows_cuda_runtime_packages",
        lambda: ["nvidia-cuda-runtime-cu12"],
    )
    monkeypatch.setattr(
        "services.subtitles.SubtitleGenerationService.prompt_cuda_runtime_choice",
        prompt_and_change_media,
    )
    monkeypatch.setattr(service._cuda_runtime_flow, "start", lambda run_id, packages: start_calls.append((run_id, list(packages))) or True)

    cuda_options = _options()
    cuda_options.device = "cuda"
    service._ui.dialog_requests[-1]["on_generate"](cuda_options)

    assert start_calls == []
    assert service._active_run is None
    assert service._service_state == SubtitleServiceState.DIALOG_OPEN


def test_begin_shutdown_requests_graceful_stop_for_active_worker():
    player = FakePlayerWindow()
    service, _store = _make_service(QWidget(), player)
    worker = FakeSubtitleWorker()

    run = _seed_active_run(service)
    run.task = SubtitlePipelineTask.SUBTITLE_GENERATION
    run.subtitle_worker = worker
    run.subtitle_thread = _RunningThread()

    pending = service.begin_shutdown()

    assert pending is True
    assert service._service_state == SubtitleServiceState.SHUTTING_DOWN
    assert run.phase == SubtitlePipelinePhase.CANCELING
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
    assert service._service_state == SubtitleServiceState.SHUTTING_DOWN
    assert service._cuda_runtime_flow.request_stop_calls == [False]
    assert service._ui.closed_generation_dialogs == 1
    assert service.is_shutdown_in_progress() is True


def test_begin_shutdown_waits_for_active_audio_probe():
    player = FakePlayerWindow()
    service, _store = _make_service(QWidget(), player)
    probe_request_id, thread, worker = _seed_active_audio_probe(service)
    finished = []
    service.shutdown_finished.connect(lambda: finished.append(True))

    pending = service.begin_shutdown()

    assert pending is True
    assert worker.cancel_calls == 1
    assert service.has_active_tasks() is True
    assert service.is_shutdown_in_progress() is True
    assert finished == []

    thread.running = False
    service._audio_probe_flow._on_probe_thread_finished(probe_request_id)

    assert service.has_active_tasks() is False
    assert service.is_shutdown_in_progress() is False
    assert finished == [True]


def test_begin_force_shutdown_requests_force_stop_for_active_worker():
    player = FakePlayerWindow()
    service, _store = _make_service(QWidget(), player)
    worker = FakeSubtitleWorker()

    run = _seed_active_run(service)
    run.task = SubtitlePipelineTask.SUBTITLE_GENERATION
    run.subtitle_worker = worker
    run.subtitle_thread = _RunningThread()

    pending = service.begin_force_shutdown()

    assert pending is True
    assert service._service_state == SubtitleServiceState.SHUTTING_DOWN
    assert worker.force_stop_calls == 1
    assert service._ui.closed_progress_dialogs == 1


def test_begin_force_shutdown_escalates_active_audio_probe():
    player = FakePlayerWindow()
    service, _store = _make_service(QWidget(), player)
    _probe_request_id, _thread, worker = _seed_active_audio_probe(service)

    assert service.begin_shutdown() is True
    assert service.begin_force_shutdown() is True

    assert worker.cancel_calls == 1
    assert worker.force_stop_calls == 1


def test_begin_force_shutdown_after_graceful_cancel_escalates_active_worker():
    player = FakePlayerWindow()
    service, _store = _make_service(QWidget(), player)
    worker = FakeSubtitleWorker()

    run = _seed_active_run(service)
    run.task = SubtitlePipelineTask.SUBTITLE_GENERATION
    run.subtitle_worker = worker
    run.subtitle_thread = _RunningThread()

    assert service.begin_shutdown() is True
    assert service.begin_force_shutdown() is True
    assert service.begin_force_shutdown() is True

    assert worker.cancel_calls == 1
    assert worker.force_stop_calls == 1
    assert run.phase == SubtitlePipelinePhase.CANCELING


def test_begin_force_shutdown_requests_force_stop_for_active_cuda_flow():
    player = FakePlayerWindow()
    service, _store = _make_service(QWidget(), player)

    run = _seed_active_run(service)
    run.task = SubtitlePipelineTask.CUDA_INSTALL
    service._cuda_runtime_flow._active = True

    pending = service.begin_force_shutdown()

    assert pending is True
    assert service._service_state == SubtitleServiceState.SHUTTING_DOWN
    assert service._cuda_runtime_flow.request_stop_calls == [True]
    assert service._ui.closed_progress_dialogs == 1


def test_begin_force_shutdown_after_graceful_cancel_escalates_cuda_flow():
    player = FakePlayerWindow()
    service, _store = _make_service(QWidget(), player)

    run = _seed_active_run(service)
    run.task = SubtitlePipelineTask.CUDA_INSTALL
    service._cuda_runtime_flow._active = True

    assert service.begin_shutdown() is True
    assert service.begin_force_shutdown() is True
    assert service.begin_force_shutdown() is True

    assert service._cuda_runtime_flow.request_stop_calls == [False, True]
    assert run.phase == SubtitlePipelinePhase.CANCELING


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


def test_active_subtitle_worker_events_are_forwarded_to_ui(monkeypatch):
    player = FakePlayerWindow()
    service, _store = _make_service(QWidget(), player)
    worker = FakeSubtitleWorker()

    run = _seed_active_run(service)
    run.subtitle_worker = worker
    monkeypatch.setattr(service, "sender", lambda: worker)

    service._on_worker_status_changed_from_worker("Working")
    service._on_worker_progress_changed_from_worker(42)
    service._on_worker_details_changed_from_worker("Details")

    assert service._ui.status_updates == ["Working"]
    assert service._ui.progress_updates == [42]
    assert service._ui.detail_updates == ["Details"]


def test_real_cuda_runtime_flow_forwards_only_active_worker_sender(monkeypatch):
    module = _load_real_module(
        "real_subtitle_cuda_runtime_flow_forwarding_test",
        "services/subtitles/SubtitleCudaRuntimeFlow.py",
    )
    parent = QWidget()
    flow = module.SubtitleCudaRuntimeFlow(parent)
    active_worker = QObject()
    stale_worker = QObject()
    calls = {
        "status": [],
        "details": [],
        "finished": [],
        "failed": [],
        "canceled": [],
    }

    flow.status_changed.connect(lambda run_id, text: calls["status"].append((run_id, text)))
    flow.details_changed.connect(lambda run_id, text: calls["details"].append((run_id, text)))
    flow.finished.connect(lambda run_id: calls["finished"].append(run_id))
    flow.failed.connect(lambda run_id, error_text: calls["failed"].append((run_id, error_text)))
    flow.canceled.connect(lambda run_id: calls["canceled"].append(run_id))

    flow._worker = active_worker
    flow._run_id = 21
    monkeypatch.setattr(flow, "sender", lambda: stale_worker)
    flow._on_worker_status_changed("Ignored")

    monkeypatch.setattr(flow, "sender", lambda: active_worker)
    flow._on_worker_status_changed("Installing")
    flow._on_worker_details_changed("Details")
    flow._on_worker_finished()
    flow._on_worker_failed("Failure")
    flow._on_worker_canceled()

    assert calls == {
        "status": [(21, "Installing")],
        "details": [(21, "Details")],
        "finished": [21],
        "failed": [(21, "Failure")],
        "canceled": [21],
    }


def test_terminal_completion_clears_active_run_and_resumes_player_ui():
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/movie.mkv"
    player.playback._request_id = 7
    store = FakeMediaStore()
    service, _store = _make_service(QWidget(), player, store)

    run = _seed_active_run(service)
    service._playback_takeover.acquire()
    service._suspend_player_ui_for_generation()

    service._on_subtitle_generation_finished(run.run_id, "C:/tmp/generated.srt", True, False)

    assert service._active_run is None
    assert service._service_state == SubtitleServiceState.IDLE
    assert service._last_result == SubtitlePipelineResult.SUCCEEDED
    assert player.resume_calls == 1
    assert player.playback.opened_subtitles == ["C:/tmp/generated.srt"]
    assert store.saved_last_open_dir == ["C:/tmp/generated.srt"]
    assert sys.modules["ui.MessageBoxService"].subtitle_created_calls == ["C:/tmp/generated.srt"]


def test_service_stores_player_ui_suspend_lease_without_mirror_state_attr():
    player = FakePlayerWindow()
    service, _store = _make_service(QWidget(), player)

    service._suspend_player_ui_for_generation()
    service._suspend_player_ui_for_generation()

    removed_mirror_attr = "_player_ui_" + "suspended_for_generation"
    assert not hasattr(service, removed_mirror_attr)
    assert service._player_ui_suspend_lease is player.suspend_leases[0]
    assert player.suspend_calls == 1


def test_repeated_playback_takeover_release_is_idempotent_for_player_ui():
    player = FakePlayerWindow()
    service, _store = _make_service(QWidget(), player)

    service._suspend_player_ui_for_generation()

    service._release_playback_takeover(resume_playback=False)
    service._release_playback_takeover(resume_playback=False)

    assert service._player_ui_suspend_lease is None
    assert player.suspend_leases[0].released is True
    assert player.resume_calls == 1


def test_shutdown_releases_player_ui_suspend_lease_without_resuming_playback():
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/movie.mkv"
    player.playback._request_id = 7
    player.playback._is_playing = True
    service, _store = _make_service(QWidget(), player)

    assert service.generate_subtitle() is True
    service._suspend_player_ui_for_generation()
    pending = service.begin_shutdown()

    assert pending is False
    assert service._player_ui_suspend_lease is None
    assert player.resume_calls == 1
    assert player.playback.pause_calls == 1
    assert player.playback.play_calls == 0


def test_shutdown_terminal_completion_releases_player_ui_suspend_lease_without_playback_resume():
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/movie.mkv"
    player.playback._request_id = 7
    player.playback._is_playing = True
    service, _store = _make_service(QWidget(), player)

    run = _seed_active_run(service)
    service._playback_takeover.acquire()
    service._suspend_player_ui_for_generation()
    service._service_state = SubtitleServiceState.SHUTTING_DOWN

    service._on_subtitle_generation_canceled(run.run_id)

    assert service._active_run is None
    assert service._player_ui_suspend_lease is None
    assert player.resume_calls == 1
    assert player.playback.pause_calls == 1
    assert player.playback.play_calls == 0
    assert player.playback.interruptions == {}


def test_shutdown_waits_for_subtitle_thread_cleanup_after_terminal_event():
    player = FakePlayerWindow()
    service, _store = _make_service(QWidget(), player)
    finished = []

    run = _seed_active_run(service)
    run.task = SubtitlePipelineTask.SUBTITLE_GENERATION
    service._pending_subtitle_thread_run_ids.add(run.run_id)
    service._service_state = SubtitleServiceState.SHUTTING_DOWN
    service.shutdown_finished.connect(lambda: finished.append(True))

    service._on_subtitle_generation_canceled(run.run_id)

    assert service._active_run is run
    assert service.is_shutdown_in_progress() is True
    assert finished == []

    service._on_background_task_thread_finished(run.run_id, SubtitlePipelineTask.SUBTITLE_GENERATION)

    assert service._active_run is None
    assert service.is_shutdown_in_progress() is False
    assert finished == [True]


def test_shutdown_waits_for_cuda_thread_cleanup_after_terminal_event():
    player = FakePlayerWindow()
    service, _store = _make_service(QWidget(), player)
    finished = []

    run = _seed_active_run(service)
    run.task = SubtitlePipelineTask.CUDA_INSTALL
    service._cuda_runtime_flow._active = True
    service._service_state = SubtitleServiceState.SHUTTING_DOWN
    service.shutdown_finished.connect(lambda: finished.append(True))

    service._on_cuda_runtime_install_canceled(run.run_id)

    assert service._active_run is run
    assert service.is_shutdown_in_progress() is True
    assert finished == []

    service._cuda_runtime_flow._active = False
    service._on_cuda_runtime_flow_thread_finished(run.run_id)

    assert service._active_run is None
    assert service.is_shutdown_in_progress() is False
    assert finished == [True]


def test_generation_dialog_cancel_releases_takeover_atomically():
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/movie.mkv"
    player.playback._request_id = 7
    player.playback._is_playing = True
    service, _store = _make_service(QWidget(), player)

    assert service.generate_subtitle() is True
    service._ui.dialog_requests[-1]["on_cancel"]()

    assert service._service_state == SubtitleServiceState.IDLE
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
    assert sys.modules["ui.MessageBoxService"].subtitle_created_context_changed_calls == ["C:/tmp/generated.srt"]


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
    assert sys.modules["ui.MessageBoxService"].subtitle_auto_load_failed_calls == ["C:/tmp/generated.srt"]


def test_generate_stays_non_blocking_while_audio_tracks_are_loading(monkeypatch):
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/movie.mkv"
    player.playback._request_id = 7
    service, _store = _make_service(QWidget(), player)

    loading_messages = []
    launch_calls = []
    monkeypatch.setattr(
        "services.subtitles.SubtitleGenerationValidationPresenter.show_audio_streams_still_loading",
        lambda _parent: loading_messages.append(True),
    )
    monkeypatch.setattr(
        service,
        "_launch_subtitle_generation",
        lambda run, options: launch_calls.append((run, options)),
    )

    assert service.generate_subtitle() is True
    assert service._audio_probe_flow.probe_state == AudioStreamProbeState.LOADING
    assert service._ui.audio_tracks_loading_calls == 1
    assert len(service._audio_probe_flow.workers) == 1

    service._ui.dialog_requests[-1]["on_generate"](_options())

    assert loading_messages == [True]
    assert launch_calls == []
    assert service._service_state == SubtitleServiceState.DIALOG_OPEN
    assert service._active_run is None
    service._audio_probe_flow.invalidate_active_request("test cleanup")


def test_generation_validation_overwrite_confirmation_is_handled_by_service(monkeypatch):
    player = FakePlayerWindow()
    service, _store = _make_service(QWidget(), player)
    confirmation_calls = []

    monkeypatch.setattr(
        "services.subtitles.SubtitleGenerationValidationPresenter.confirm_overwrite_subtitle",
        lambda _parent, output_path: confirmation_calls.append(output_path) or False,
    )

    result = SubtitleGenerationValidationResult(
        is_valid=False,
        reason=SubtitleGenerationValidationFailure.OVERWRITE_CONFIRMATION_REQUIRED,
        output_path="C:/tmp/existing.srt",
    )

    assert service._validation_presenter.confirm_or_show_failure(result) is False
    assert confirmation_calls == ["C:/tmp/existing.srt"]


def test_generate_starts_normally_after_audio_probe_ready(monkeypatch):
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/movie.mkv"
    player.playback._request_id = 7
    service, _store = _make_service(QWidget(), player)

    launches = []
    service.generate_subtitle()

    probe_request_id = service._audio_probe_flow.current_probe_request_id
    service._audio_probe_flow._on_probe_finished(
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


def test_generation_aborts_when_playback_context_changes_before_launch(monkeypatch):
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/movie.mkv"
    player.playback._request_id = 7
    service, _store = _make_service(QWidget(), player)

    launches = []
    service.generate_subtitle()

    probe_request_id = service._audio_probe_flow.current_probe_request_id
    service._audio_probe_flow._on_probe_finished(
        probe_request_id,
        player.playback._media_path,
        [_AudioStream(1, "Audio 1")],
    )
    monkeypatch.setattr(
        service,
        "_launch_subtitle_generation",
        lambda run, options: launches.append((run, options)),
    )

    original_validate = service._preflight.validate_generation_request

    def validate_and_change_media(*args, **kwargs):
        result = original_validate(*args, **kwargs)
        player.playback._media_path = "C:/media/other.mkv"
        player.playback._request_id = 8
        return result

    monkeypatch.setattr(service._preflight, "validate_generation_request", validate_and_change_media)

    service._ui.dialog_requests[-1]["on_generate"](_options())

    assert launches == []
    assert service._active_run is None
    assert service._service_state == SubtitleServiceState.DIALOG_OPEN


def test_generation_dialog_uses_default_only_when_player_reports_single_audio_track(monkeypatch):
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/movie.mkv"
    player.playback._request_id = 7
    player.get_audio_tracks = lambda: [(1, "Audio 1")]
    service, _store = _make_service(QWidget(), player)

    launches = []
    monkeypatch.setattr(
        service,
        "_launch_subtitle_generation",
        lambda run, options: launches.append((run, options)),
    )

    assert service.generate_subtitle() is True

    assert service._audio_probe_flow.probe_state == AudioStreamProbeState.READY
    assert service._audio_probe_flow.cached_audio_streams == []
    assert service._audio_probe_flow.workers == {}
    assert service._ui.audio_tracks_loading_calls == 0
    assert service._ui.applied_audio_tracks == [
        {
            "audio_tracks": [(None, "Current / default")],
            "selected_track_id": None,
            "selector_enabled": False,
            "generate_enabled": True,
        }
    ]

    service._ui.dialog_requests[-1]["on_generate"](_options())

    assert len(launches) == 1
    assert launches[0][1].audio_stream_index is None


def test_generate_reuses_cached_audio_probe_failure_without_sync_probe(monkeypatch):
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/movie.mkv"
    player.playback._request_id = 7
    service, _store = _make_service(QWidget(), player)

    warning_messages = []
    launch_calls = []

    monkeypatch.setattr(
        "services.subtitles.SubtitleGenerationAudioProbeFlow.show_audio_stream_inspection_warning",
        lambda _parent, reason: warning_messages.append(reason),
    )
    monkeypatch.setattr(
        service,
        "_launch_subtitle_generation",
        lambda run, options: launch_calls.append((run, options)),
    )

    service.generate_subtitle()
    probe_request_id = service._audio_probe_flow.current_probe_request_id
    service._audio_probe_flow._on_probe_failed(probe_request_id, player.playback._media_path, "probe failed")

    service._ui.dialog_requests[-1]["on_generate"](_options())

    assert warning_messages == ["probe failed"]
    assert len(launch_calls) == 1
    assert launch_calls[0][1].audio_stream_index is None


def test_stale_audio_probe_result_is_ignored_after_dialog_close():
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/movie.mkv"
    service, _store = _make_service(QWidget(), player)

    service.generate_subtitle()
    probe_request_id = service._audio_probe_flow.current_probe_request_id
    service._ui.dialog_requests[-1]["on_cancel"]()

    service._audio_probe_flow._on_probe_finished(
        probe_request_id,
        player.playback._media_path,
        [_AudioStream(3, "Late track")],
    )

    assert service._ui.applied_audio_tracks == []
    assert service._audio_probe_flow.probe_state == AudioStreamProbeState.IDLE
    assert service._service_state == SubtitleServiceState.IDLE


def test_stale_audio_probe_result_is_ignored_after_dialog_reopen():
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/movie.mkv"
    service, _store = _make_service(QWidget(), player)

    service.generate_subtitle()
    first_probe_request_id = service._audio_probe_flow.current_probe_request_id
    service._ui.dialog_requests[-1]["on_cancel"]()

    service.generate_subtitle()
    second_probe_request_id = service._audio_probe_flow.current_probe_request_id

    service._audio_probe_flow._on_probe_finished(
        first_probe_request_id,
        player.playback._media_path,
        [_AudioStream(1, "Old track")],
    )
    service._audio_probe_flow._on_probe_finished(
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

    monkeypatch.setattr(
        preflight,
        "_validate_output_path",
        lambda _options: module.SubtitleGenerationValidationResult(is_valid=True),
    )

    result = preflight.validate_generation_request(
        "C:/media/movie.mkv",
        options,
        probe_state=module.AudioStreamProbeState.IDLE,
    )
    assert result.is_valid is False
    assert result.reason == module.SubtitleGenerationValidationFailure.AUDIO_STREAMS_STILL_LOADING

    result = preflight.validate_generation_request(
        "C:/media/movie.mkv",
        options,
        probe_state=module.AudioStreamProbeState.LOADING,
    )
    assert result.is_valid is False
    assert result.reason == module.SubtitleGenerationValidationFailure.AUDIO_STREAMS_STILL_LOADING

    result = preflight.validate_generation_request(
        "C:/media/movie.mkv",
        options,
        probe_state=module.AudioStreamProbeState.FAILED,
        probe_error="cached failure",
    )
    assert result.is_valid is True

    selected_track_options = _options()
    selected_track_options.audio_stream_index = 1
    result = preflight.validate_generation_request(
        "C:/media/movie.mkv",
        selected_track_options,
        probe_state=module.AudioStreamProbeState.FAILED,
        probe_error="cached failure",
    )
    assert result.is_valid is False
    assert result.reason == module.SubtitleGenerationValidationFailure.AUDIO_STREAM_INSPECTION_FAILED
    assert result.formatted_reason == "cached failure"


def test_real_preflight_returns_output_path_failure_reasons(monkeypatch, workspace_tmp_path):
    module = _load_real_module(
        "real_subtitle_generation_preflight_output_test",
        "services/subtitles/SubtitleGenerationPreflight.py",
    )
    preflight = module.SubtitleGenerationPreflight(QWidget())

    result = preflight.validate_generation_request(
        "C:/media/movie.mkv",
        _options(output_path=" "),
        probe_state=module.AudioStreamProbeState.READY,
    )
    assert result.is_valid is False
    assert result.reason == module.SubtitleGenerationValidationFailure.EMPTY_OUTPUT_PATH

    monkeypatch.setattr(preflight, "_preflight_subtitle_output_path", lambda _path: "not writable")
    result = preflight.validate_generation_request(
        "C:/media/movie.mkv",
        _options(output_path=str(workspace_tmp_path / "blocked.srt")),
        probe_state=module.AudioStreamProbeState.READY,
    )
    assert result.is_valid is False
    assert result.reason == module.SubtitleGenerationValidationFailure.OUTPUT_PATH_UNAVAILABLE
    assert result.preflight_error == "not writable"

    monkeypatch.setattr(preflight, "_preflight_subtitle_output_path", lambda _path: None)
    existing_output = workspace_tmp_path / "existing.srt"
    existing_output.write_text("old")
    result = preflight.validate_generation_request(
        "C:/media/movie.mkv",
        _options(output_path=str(existing_output)),
        probe_state=module.AudioStreamProbeState.READY,
    )
    assert result.is_valid is False
    assert result.reason == module.SubtitleGenerationValidationFailure.OVERWRITE_CONFIRMATION_REQUIRED
    assert result.output_path == str(existing_output)


def test_real_preflight_returns_audio_selection_failure_reasons(monkeypatch):
    module = _load_real_module(
        "real_subtitle_generation_preflight_audio_selection_test",
        "services/subtitles/SubtitleGenerationPreflight.py",
    )
    preflight = module.SubtitleGenerationPreflight(QWidget())
    selected_track_options = _options()
    selected_track_options.audio_stream_index = 2

    monkeypatch.setattr(
        preflight,
        "_validate_output_path",
        lambda _options: module.SubtitleGenerationValidationResult(is_valid=True),
    )

    result = preflight.validate_generation_request(
        "C:/media/movie.mkv",
        selected_track_options,
        probe_state=module.AudioStreamProbeState.READY,
        audio_streams=[],
    )
    assert result.is_valid is False
    assert result.reason == module.SubtitleGenerationValidationFailure.NO_AUDIO_STREAMS_FOUND

    result = preflight.validate_generation_request(
        "C:/media/movie.mkv",
        selected_track_options,
        probe_state=module.AudioStreamProbeState.READY,
        audio_streams=[_AudioStream(1, "Audio 1")],
    )
    assert result.is_valid is False
    assert result.reason == module.SubtitleGenerationValidationFailure.AUDIO_STREAM_NO_LONGER_AVAILABLE


def test_real_audio_stream_probe_worker_emits_finished_with_list_result(monkeypatch):
    module = _load_real_module(
        "real_subtitle_generation_workers_test",
        "services/subtitles/SubtitleGenerationWorkers.py",
    )

    monkeypatch.setattr(
        module.AudioStreamProbeWorker,
        "_probe_audio_streams",
        lambda self: (_AudioStream(1, f"{self._media_path}-track"),),
    )

    worker = module.AudioStreamProbeWorker(11, "C:/media/movie.mkv")
    finished_calls = []
    failed_calls = []
    worker.finished.connect(lambda request_id, media_path, audio_streams: finished_calls.append((request_id, media_path, audio_streams)))
    worker.failed.connect(lambda request_id, media_path, reason: failed_calls.append((request_id, media_path, reason)))

    worker.run()

    assert finished_calls == [(11, "C:/media/movie.mkv", [_AudioStream(1, "C:/media/movie.mkv-track")])]
    assert failed_calls == []


def test_real_audio_stream_probe_worker_emits_failure_on_probe_error(monkeypatch):
    module = _load_real_module(
        "real_subtitle_generation_workers_failure_test",
        "services/subtitles/SubtitleGenerationWorkers.py",
    )

    monkeypatch.setattr(
        module.AudioStreamProbeWorker,
        "_probe_audio_streams",
        lambda _self: (_ for _ in ()).throw(RuntimeError("probe boom")),
    )

    worker = module.AudioStreamProbeWorker(12, "C:/media/broken.mkv")
    finished_calls = []
    failed_calls = []
    worker.finished.connect(lambda request_id, media_path, audio_streams: finished_calls.append((request_id, media_path, audio_streams)))
    worker.failed.connect(lambda request_id, media_path, reason: failed_calls.append((request_id, media_path, reason)))

    worker.run()

    assert finished_calls == []
    assert failed_calls == [(12, "C:/media/broken.mkv", "probe boom")]


def test_real_audio_stream_probe_worker_is_qthread_driven():
    module = _load_real_module(
        "real_subtitle_generation_workers_start_test",
        "services/subtitles/SubtitleGenerationWorkers.py",
    )

    worker = module.AudioStreamProbeWorker(13, "C:/media/movie.mkv")

    assert hasattr(worker, "run")
    assert not hasattr(worker, "start")
    assert not hasattr(worker, "_thread")


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


def test_subtitle_maker_keeps_legacy_public_exports():
    module = _load_real_module(
        "real_subtitle_maker_exports_test",
        "services/subtitles/SubtitleMaker.py",
    )

    assert module.SubtitleSegment.__name__ == "SubtitleSegment"
    assert module.AudioStreamInfo.__name__ == "AudioStreamInfo"
    assert module.SubtitleGenerationCanceledError.__name__ == "SubtitleGenerationCanceledError"
    assert module.SubtitleGenerationEmptyResultError.__name__ == "SubtitleGenerationEmptyResultError"
    assert callable(module.probe_audio_streams)
    assert callable(module.get_missing_windows_cuda_runtime_packages)


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
    audio_flow_module = sys.modules["services.subtitles.SubtitleGenerationAudioProbeFlow"]

    class _Signal:
        def __init__(self):
            self._callbacks = []

        def connect(self, callback, *_args):
            self._callbacks.append(callback)

        def emit(self, *args):
            for callback in list(self._callbacks):
                try:
                    callback(*args)
                except TypeError:
                    callback()

    scheduled_starts = []

    class _DeferredThread:
        def __init__(self, _parent=None):
            self.started = _Signal()
            self.finished = _Signal()
            self._running = False

        def start(self):
            self._running = True
            scheduled_starts.append(self.started.emit)

        def quit(self):
            if not self._running:
                return
            self._running = False
            self.finished.emit()

        def isRunning(self):
            return self._running

        def deleteLater(self):
            return None

    class _DeferredProbeWorker:
        def __init__(self, probe_request_id, media_path):
            self._probe_request_id = probe_request_id
            self._media_path = media_path
            self.finished = _Signal()
            self.failed = _Signal()
            self.canceled = _Signal()

        def moveToThread(self, _thread):
            return None

        def run(self):
            self.finished.emit(
                self._probe_request_id,
                self._media_path,
                [_AudioStream(1, f"{self._media_path}-track-{len(scheduled_starts)}")],
            )

        def cancel(self):
            self.canceled.emit(self._probe_request_id)

        def force_stop(self):
            self.canceled.emit(self._probe_request_id)

        def deleteLater(self):
            return None

    monkeypatch.setattr(audio_flow_module, "QThread", _DeferredThread)
    monkeypatch.setattr(audio_flow_module, "AudioStreamProbeWorker", _DeferredProbeWorker)

    app = QApplication.instance() or QApplication([])
    player = FakePlayerWindow()
    player.playback._media_path = "C:/media/movie.mkv"
    player.playback._request_id = 7
    service, _store = _make_service(QWidget(), player)

    assert service.generate_subtitle() is True
    app.processEvents()
    first_probe_request_id = service._audio_probe_flow.current_probe_request_id
    assert len(scheduled_starts) == 1

    service._ui.dialog_requests[-1]["on_cancel"]()
    assert service.generate_subtitle() is True
    app.processEvents()
    second_probe_request_id = service._audio_probe_flow.current_probe_request_id
    assert second_probe_request_id != first_probe_request_id
    assert len(scheduled_starts) == 2

    scheduled_starts[0]()
    app.processEvents()
    assert service._ui.applied_audio_tracks == []
    assert service._audio_probe_flow.current_probe_request_id == second_probe_request_id

    scheduled_starts[1]()
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
