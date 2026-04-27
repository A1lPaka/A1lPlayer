import os
import shutil
import sys
import types
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from uuid import uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication


def _install_playback_engine_stub():
    if "services.PlaybackEngine" in sys.modules:
        return

    playback_engine = types.ModuleType("services.PlaybackEngine")

    class PlaybackService(QObject):
        playing = Signal(int)
        paused = Signal(int)
        stopped = Signal(int)
        media_ended = Signal(int)
        playback_error = Signal(int, str, str)
        video_geometry_changed = Signal(int, int)

        def __init__(self, parent=None):
            super().__init__(parent)
            self._next_request_id = 1
            self.loaded_media = []
            self.play_calls = 0
            self.pause_calls = 0
            self.stop_calls = 0
            self.shutdown_calls = 0
            self.sync_calls = 0
            self.subtitle_opens = []
            self._time = 0
            self._length = 0
            self._is_playing = False
            self._is_shutdown = False

        def load_media(self, media_path: str) -> int:
            request_id = self._next_request_id
            self._next_request_id += 1
            self.loaded_media.append(media_path)
            return request_id

        def sync_audio_to_player(self):
            self.sync_calls += 1

        def play(self):
            self.play_calls += 1
            self._is_playing = True

        def pause(self):
            self.pause_calls += 1
            self._is_playing = False

        def stop(self):
            self.stop_calls += 1
            self._is_playing = False

        def shutdown(self):
            self.shutdown_calls += 1
            self._is_playing = False
            self._is_shutdown = True

        def is_playing(self) -> bool:
            return self._is_playing

        def get_time(self) -> int:
            return self._time

        def set_time(self, value: int):
            self._time = int(value)

        def get_length(self) -> int:
            return self._length

        def set_length(self, value: int):
            self._length = int(value)

        def is_seekable(self) -> bool:
            return True

        def set_position(self, _value: float):
            return None

        def get_rate(self) -> float:
            return 1.0

        def set_rate(self, _speed: float):
            return True

        def get_audio_tracks(self):
            return []

        def get_current_audio_track(self) -> int:
            return -1

        def set_audio_track(self, _track_id: int) -> bool:
            return True

        def get_audio_devices(self):
            return []

        def get_current_audio_device(self) -> str:
            return "__default__"

        def set_audio_device(self, _device_id: str) -> bool:
            return True

        def get_current_audio_mode(self) -> str:
            return "stereo"

        def set_audio_mode(self, _channel: str) -> bool:
            return True

        def get_subtitle_tracks(self):
            return []

        def get_current_subtitle_track(self) -> int:
            return -1

        def set_subtitle_track(self, _track_id: int) -> bool:
            return True

        def open_subtitle_file(self, subtitle_path: str) -> bool:
            self.subtitle_opens.append(subtitle_path)
            return True

        def get_desired_volume(self) -> int:
            return 100

        def is_muted(self) -> bool:
            return False

        def set_volume(self, _volume: int):
            return None

        def set_last_volume_before_mute(self, _volume: int):
            return None

        def get_last_volume_before_mute(self) -> int:
            return 100

        def set_muted(self, _muted: bool):
            return None

        def bind_video_output(self, _win_id: int):
            return None

        def get_video_dimensions(self):
            return None

    playback_engine.PlaybackService = PlaybackService
    sys.modules["services.PlaybackEngine"] = playback_engine


def _install_player_window_stub():
    if "ui.PlayerWindow" in sys.modules:
        return

    player_window = types.ModuleType("ui.PlayerWindow")

    class PlayerWindow(QObject):
        pass

    player_window.PlayerWindow = PlayerWindow
    sys.modules["ui.PlayerWindow"] = player_window


def _install_message_box_stub():
    if "ui.MessageBoxService" in sys.modules:
        return

    message_box = types.ModuleType("ui.MessageBoxService")
    message_box.subtitle_created_calls = []
    message_box.subtitle_created_fallback_calls = []
    message_box.subtitle_created_context_changed_calls = []
    message_box.subtitle_auto_load_failed_calls = []
    message_box.subtitle_generation_failures = []
    message_box.subtitle_generation_canceled_calls = 0
    message_box.cuda_runtime_install_failures = []
    message_box.cuda_runtime_install_canceled_calls = 0

    def prompt_cuda_runtime_choice(_parent, _packages):
        return "cancel"

    def show_subtitle_generation_already_running(_parent):
        return None

    def show_audio_streams_still_loading(_parent):
        return None

    def show_audio_stream_inspection_failed(_parent, _reason):
        return None

    def show_audio_stream_inspection_warning(_parent, _reason):
        return None

    def confirm_overwrite_subtitle(_parent, _output_path):
        return True

    def show_audio_stream_no_longer_available(_parent):
        return None

    def show_choose_output_path_first(_parent):
        return None

    def show_no_audio_streams_found(_parent):
        return None

    def show_subtitle_output_path_unavailable(_parent, _output_path, _reason=None):
        return None

    def show_subtitle_created_not_loaded_due_to_context_change(_parent, output_path):
        message_box.subtitle_created_context_changed_calls.append(output_path)

    def show_subtitle_auto_load_failed(_parent, output_path):
        message_box.subtitle_auto_load_failed_calls.append(output_path)

    def show_subtitle_created(_parent, output_path):
        message_box.subtitle_created_calls.append(output_path)

    def show_subtitle_created_with_fallback_name(_parent, requested_output_path, actual_output_path):
        message_box.subtitle_created_fallback_calls.append((requested_output_path, actual_output_path))

    def show_subtitle_generation_failed(_parent, error_text):
        message_box.subtitle_generation_failures.append(error_text)

    def show_subtitle_generation_canceled(_parent):
        message_box.subtitle_generation_canceled_calls += 1

    def show_cuda_runtime_install_failed(_parent, error_text):
        message_box.cuda_runtime_install_failures.append(error_text)

    def show_cuda_runtime_install_canceled(_parent):
        message_box.cuda_runtime_install_canceled_calls += 1

    def prompt_force_close_background_tasks(_parent, on_wait, on_force_close):
        class _Dialog(QObject):
            destroyed = Signal(object)

            def close(self):
                self.destroyed.emit(None)

        dialog = _Dialog()
        dialog.on_wait = on_wait
        dialog.on_force_close = on_force_close
        return dialog

    def show_force_close_still_running(_parent):
        return None

    def confirm_resume_playback(_parent, _path, _position_ms):
        return False

    def show_media_access_failed(_parent, _path):
        return None

    def show_no_supported_media_found(_parent, _path):
        return None

    def show_open_subtitle_failed(_parent):
        return None

    message_box.prompt_cuda_runtime_choice = prompt_cuda_runtime_choice
    message_box.show_subtitle_generation_already_running = show_subtitle_generation_already_running
    message_box.show_audio_streams_still_loading = show_audio_streams_still_loading
    message_box.show_audio_stream_inspection_failed = show_audio_stream_inspection_failed
    message_box.show_audio_stream_inspection_warning = show_audio_stream_inspection_warning
    message_box.confirm_overwrite_subtitle = confirm_overwrite_subtitle
    message_box.show_audio_stream_no_longer_available = show_audio_stream_no_longer_available
    message_box.show_choose_output_path_first = show_choose_output_path_first
    message_box.show_no_audio_streams_found = show_no_audio_streams_found
    message_box.show_subtitle_output_path_unavailable = show_subtitle_output_path_unavailable
    message_box.show_subtitle_created_not_loaded_due_to_context_change = show_subtitle_created_not_loaded_due_to_context_change
    message_box.show_subtitle_auto_load_failed = show_subtitle_auto_load_failed
    message_box.show_subtitle_created = show_subtitle_created
    message_box.show_subtitle_created_with_fallback_name = show_subtitle_created_with_fallback_name
    message_box.show_subtitle_generation_failed = show_subtitle_generation_failed
    message_box.show_subtitle_generation_canceled = show_subtitle_generation_canceled
    message_box.show_cuda_runtime_install_failed = show_cuda_runtime_install_failed
    message_box.show_cuda_runtime_install_canceled = show_cuda_runtime_install_canceled
    message_box.prompt_force_close_background_tasks = prompt_force_close_background_tasks
    message_box.show_force_close_still_running = show_force_close_still_running
    message_box.confirm_resume_playback = confirm_resume_playback
    message_box.show_media_access_failed = show_media_access_failed
    message_box.show_no_supported_media_found = show_no_supported_media_found
    message_box.show_open_subtitle_failed = show_open_subtitle_failed
    sys.modules["ui.MessageBoxService"] = message_box


def _install_subtitle_service_stubs():
    if "services.subtitles.SubtitleGenerationUiCoordinator" not in sys.modules:
        ui_module = types.ModuleType("services.subtitles.SubtitleGenerationUiCoordinator")

        class SubtitleGenerationUiCoordinator:
            def __init__(self, parent, theme_color_getter):
                self.parent = parent
                self.theme_color_getter = theme_color_getter
                self.dialog_requests = []
                self.progress_requests = []
                self.focus_calls = 0
                self.status_updates = []
                self.progress_updates = []
                self.detail_updates = []
                self.cancel_pending_calls = 0
                self.cuda_cancel_pending_calls = 0
                self.closed_generation_dialogs = 0
                self.closed_progress_dialogs = 0
                self.audio_tracks_loading_calls = 0
                self.applied_audio_tracks = []
                self.has_generation_dialog_value = False

            def open_generation_dialog(self, media_path, on_generate, on_cancel):
                self.has_generation_dialog_value = True
                self.dialog_requests.append(
                    {
                        "media_path": media_path,
                        "on_generate": on_generate,
                        "on_cancel": on_cancel,
                    }
                )

            def set_generation_dialog_audio_tracks_loading(self):
                self.audio_tracks_loading_calls += 1

            def apply_generation_dialog_audio_tracks(
                self,
                audio_tracks,
                *,
                selected_track_id=None,
                selector_enabled=False,
                generate_enabled=False,
            ):
                self.applied_audio_tracks.append(
                    {
                        "audio_tracks": list(audio_tracks),
                        "selected_track_id": selected_track_id,
                        "selector_enabled": selector_enabled,
                        "generate_enabled": generate_enabled,
                    }
                )

            def has_generation_dialog(self):
                return self.has_generation_dialog_value

            def focus_active_dialog(self):
                self.focus_calls += 1

            def open_generation_progress(self, options, on_cancel):
                self.progress_requests.append({"options": options, "on_cancel": on_cancel})

            def open_cuda_install_progress(self, missing_packages, on_cancel):
                self.progress_requests.append({"options": list(missing_packages), "on_cancel": on_cancel})

            def update_progress_status(self, text):
                self.status_updates.append(text)

            def update_progress(self, value):
                self.progress_updates.append(value)

            def update_progress_details(self, text):
                self.detail_updates.append(text)

            def close_generation_dialog(self):
                self.closed_generation_dialogs += 1
                self.has_generation_dialog_value = False

            def close_progress_dialog(self):
                self.closed_progress_dialogs += 1

            def show_subtitle_cancel_pending(self):
                self.cancel_pending_calls += 1

            def show_cuda_install_cancel_pending(self):
                self.cuda_cancel_pending_calls += 1

        ui_module.SubtitleGenerationUiCoordinator = SubtitleGenerationUiCoordinator
        sys.modules["services.subtitles.SubtitleGenerationUiCoordinator"] = ui_module

    if "services.subtitles.SubtitleGenerationPreflight" not in sys.modules:
        preflight_module = types.ModuleType("services.subtitles.SubtitleGenerationPreflight")

        class AudioStreamProbeState(Enum):
            IDLE = auto()
            LOADING = auto()
            READY = auto()
            FAILED = auto()

        class SubtitleGenerationValidationFailure(Enum):
            EMPTY_OUTPUT_PATH = auto()
            OUTPUT_PATH_UNAVAILABLE = auto()
            OVERWRITE_CONFIRMATION_REQUIRED = auto()
            AUDIO_STREAMS_STILL_LOADING = auto()
            AUDIO_STREAM_INSPECTION_FAILED = auto()
            NO_AUDIO_STREAMS_FOUND = auto()
            AUDIO_STREAM_NO_LONGER_AVAILABLE = auto()

        class SubtitleGenerationValidationResult:
            def __init__(
                self,
                is_valid=True,
                reason=None,
                output_path=None,
                preflight_error=None,
                probe_error=None,
                formatted_reason=None,
            ):
                self.is_valid = is_valid
                self.reason = reason
                self.output_path = output_path
                self.preflight_error = preflight_error
                self.probe_error = probe_error
                self.formatted_reason = formatted_reason

        class SubtitleGenerationPreflight:
            def __init__(self, parent):
                self.parent = parent
                self._validation_results = {}

            def build_audio_track_choices(self, audio_streams):
                return [(None, "Current / default"), *[(stream.stream_index, stream.label) for stream in audio_streams]]

            def format_audio_stream_probe_error(self, reason):
                return str(reason)

            def validate_generation_request(self, media_path, _options, *, probe_state, audio_streams=None, probe_error=None):
                override = self._validation_results.get(media_path)
                if override is not None:
                    return SubtitleGenerationValidationResult(override)

                if probe_state in (AudioStreamProbeState.IDLE, AudioStreamProbeState.LOADING):
                    return SubtitleGenerationValidationResult(
                        False,
                        reason=SubtitleGenerationValidationFailure.AUDIO_STREAMS_STILL_LOADING,
                    )
                if probe_state == AudioStreamProbeState.FAILED:
                    if _options.audio_stream_index is None:
                        return SubtitleGenerationValidationResult(True)
                    formatted_reason = self.format_audio_stream_probe_error(
                        probe_error or "Audio stream inspection failed."
                    )
                    return SubtitleGenerationValidationResult(
                        False,
                        reason=SubtitleGenerationValidationFailure.AUDIO_STREAM_INSPECTION_FAILED,
                        probe_error=probe_error,
                        formatted_reason=formatted_reason,
                    )
                return SubtitleGenerationValidationResult(True)

        preflight_module.SubtitleGenerationPreflight = SubtitleGenerationPreflight
        preflight_module.AudioStreamProbeState = AudioStreamProbeState
        preflight_module.SubtitleGenerationValidationFailure = SubtitleGenerationValidationFailure
        preflight_module.SubtitleGenerationValidationResult = SubtitleGenerationValidationResult
        sys.modules["services.subtitles.SubtitleGenerationPreflight"] = preflight_module

    if "services.subtitles.SubtitleGenerationWorkers" not in sys.modules:
        workers_module = types.ModuleType("services.subtitles.SubtitleGenerationWorkers")

        class AudioStreamProbeWorker(QObject):
            finished = Signal(int, str, object)
            failed = Signal(int, str, str)
            canceled = Signal(int)

            def __init__(self, probe_request_id, media_path):
                super().__init__()
                self.probe_request_id = probe_request_id
                self.media_path = media_path
                self.start_calls = 0
                self.cancel_calls = 0
                self.force_stop_calls = 0

            def start(self):
                self.start_calls += 1

            def run(self):
                self.start_calls += 1

            def cancel(self):
                self.cancel_calls += 1
                self.canceled.emit(self.probe_request_id)

            def force_stop(self):
                self.force_stop_calls += 1
                self.canceled.emit(self.probe_request_id)

        class SubtitleGenerationWorker(QObject):
            status_changed = Signal(str)
            progress_changed = Signal(int)
            details_changed = Signal(str)
            finished = Signal(str, bool, bool)
            failed = Signal(str, str)
            canceled = Signal()

            def __init__(self, run_id, media_path, options):
                super().__init__()
                self.run_id = run_id
                self.media_path = media_path
                self.options = options
                self.cancel_calls = 0
                self.force_stop_calls = 0

            def run(self):
                return None

            def cancel(self):
                self.cancel_calls += 1

            def force_stop(self):
                self.force_stop_calls += 1

        workers_module.AudioStreamProbeWorker = AudioStreamProbeWorker
        workers_module.SubtitleGenerationWorker = SubtitleGenerationWorker
        sys.modules["services.subtitles.SubtitleGenerationWorkers"] = workers_module

    if "services.subtitles.SubtitleCudaRuntimeFlow" not in sys.modules:
        cuda_module = types.ModuleType("services.subtitles.SubtitleCudaRuntimeFlow")

        class SubtitleCudaRuntimeFlow(QObject):
            status_changed = Signal(int, str)
            details_changed = Signal(int, str)
            finished = Signal(int)
            failed = Signal(int, str)
            canceled = Signal(int)
            thread_finished = Signal(int)

            def __init__(self, parent):
                super().__init__(parent)
                self._active = False
                self._cancel_requested = False
                self.cancel_calls = 0
                self.request_stop_calls = []

            def start(self, _run_id, _missing_packages):
                self._active = True
                self._cancel_requested = False
                return True

            def is_active(self):
                return self._active

            def cancel(self):
                self.cancel_calls += 1

            def request_stop(self, force=False):
                if not self._active:
                    return False
                if not force and self._cancel_requested:
                    return False
                if not force:
                    self._cancel_requested = True
                self.request_stop_calls.append(force)
                return True

        cuda_module.SubtitleCudaRuntimeFlow = SubtitleCudaRuntimeFlow
        sys.modules["services.subtitles.SubtitleCudaRuntimeFlow"] = cuda_module

    if "services.subtitles.SubtitleMaker" not in sys.modules:
        maker_module = types.ModuleType("services.subtitles.SubtitleMaker")

        def get_missing_windows_cuda_runtime_packages():
            return []

        def probe_audio_streams(_media_path):
            raise AssertionError("sync probe_audio_streams() should not be called in service/preflight tests")

        maker_module.get_missing_windows_cuda_runtime_packages = get_missing_windows_cuda_runtime_packages
        maker_module.probe_audio_streams = probe_audio_streams
        sys.modules["services.subtitles.SubtitleMaker"] = maker_module


_install_playback_engine_stub()
_install_player_window_stub()
_install_message_box_stub()
_install_subtitle_service_stubs()


@dataclass
class ValidationResult:
    is_valid: bool = True


def pytest_configure():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def pytest_sessionstart(session):
    QApplication.instance() or QApplication([])


def pytest_runtest_teardown(item, nextitem):
    QApplication.processEvents()


def pytest_unconfigure(config):
    QApplication.processEvents()


def pytest_generate_tests(metafunc):
    return None


def pytest_report_header(config):
    return "Qt offscreen test harness enabled"


import pytest


@pytest.fixture
def workspace_tmp_path():
    root = Path(__file__).resolve().parent / "_tmp"
    root.mkdir(exist_ok=True)
    case_dir = root / uuid4().hex
    case_dir.mkdir()
    try:
        yield case_dir
    finally:
        shutil.rmtree(case_dir, ignore_errors=True)
