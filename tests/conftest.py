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
            self.sync_calls = 0
            self.subtitle_opens = []
            self._time = 0
            self._length = 0
            self._is_playing = False

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

    def prompt_cuda_runtime_choice(_parent, _packages):
        return "cancel"

    def show_subtitle_generation_already_running(_parent):
        return None

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

    def show_open_subtitle_failed(_parent):
        return None

    message_box.prompt_cuda_runtime_choice = prompt_cuda_runtime_choice
    message_box.show_subtitle_generation_already_running = show_subtitle_generation_already_running
    message_box.prompt_force_close_background_tasks = prompt_force_close_background_tasks
    message_box.show_force_close_still_running = show_force_close_still_running
    message_box.confirm_resume_playback = confirm_resume_playback
    message_box.show_media_access_failed = show_media_access_failed
    message_box.show_open_subtitle_failed = show_open_subtitle_failed
    sys.modules["ui.MessageBoxService"] = message_box


def _install_subtitle_service_stubs():
    if "services.SubtitleGenerationUiCoordinator" not in sys.modules:
        ui_module = types.ModuleType("services.SubtitleGenerationUiCoordinator")

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
                self.closed_generation_dialogs = 0
                self.closed_progress_dialogs = 0

            def open_generation_dialog(self, media_path, tracks, on_generate, on_cancel):
                self.dialog_requests.append(
                    {
                        "media_path": media_path,
                        "tracks": tracks,
                        "on_generate": on_generate,
                        "on_cancel": on_cancel,
                    }
                )

            def focus_active_dialog(self):
                self.focus_calls += 1

            def open_generation_progress(self, options, on_cancel):
                self.progress_requests.append({"options": options, "on_cancel": on_cancel})

            def update_progress_status(self, text):
                self.status_updates.append(text)

            def update_progress(self, value):
                self.progress_updates.append(value)

            def update_progress_details(self, text):
                self.detail_updates.append(text)

            def close_generation_dialog(self):
                self.closed_generation_dialogs += 1

            def close_progress_dialog(self):
                self.closed_progress_dialogs += 1

            def show_subtitle_cancel_pending(self):
                self.cancel_pending_calls += 1

        ui_module.SubtitleGenerationUiCoordinator = SubtitleGenerationUiCoordinator
        sys.modules["services.SubtitleGenerationUiCoordinator"] = ui_module

    if "services.SubtitleGenerationPreflight" not in sys.modules:
        preflight_module = types.ModuleType("services.SubtitleGenerationPreflight")

        class _ValidationResult:
            def __init__(self, is_valid=True):
                self.is_valid = is_valid

        class SubtitleGenerationPreflight:
            def __init__(self, parent):
                self.parent = parent

            def build_generation_audio_tracks(self, _media_path):
                return []

            def validate_generation_request(self, _media_path, _options):
                return _ValidationResult(True)

        preflight_module.SubtitleGenerationPreflight = SubtitleGenerationPreflight
        sys.modules["services.SubtitleGenerationPreflight"] = preflight_module

    if "services.SubtitleGenerationOutcomeHandler" not in sys.modules:
        outcomes_module = types.ModuleType("services.SubtitleGenerationOutcomeHandler")

        class SubtitleAutoOpenOutcome(Enum):
            LOADED = auto()
            CONTEXT_CHANGED = auto()
            LOAD_FAILED = auto()

        class SubtitleGenerationOutcomeHandler:
            def __init__(self, parent):
                self.parent = parent
                self.successes = []
                self.failures = []
                self.canceled_calls = 0
                self.cuda_failures = []
                self.cuda_canceled_calls = 0

            def show_generation_success(self, output_path, auto_open_outcome):
                self.successes.append((output_path, auto_open_outcome))

            def show_generation_failed(self, error_text):
                self.failures.append(error_text)

            def show_generation_canceled(self):
                self.canceled_calls += 1

            def show_cuda_install_failed(self, error_text):
                self.cuda_failures.append(error_text)

            def show_cuda_install_canceled(self):
                self.cuda_canceled_calls += 1

        outcomes_module.SubtitleAutoOpenOutcome = SubtitleAutoOpenOutcome
        outcomes_module.SubtitleGenerationOutcomeHandler = SubtitleGenerationOutcomeHandler
        sys.modules["services.SubtitleGenerationOutcomeHandler"] = outcomes_module

    if "services.SubtitleGenerationWorkers" not in sys.modules:
        workers_module = types.ModuleType("services.SubtitleGenerationWorkers")

        class SubtitleGenerationWorker(QObject):
            status_changed = Signal(str)
            progress_changed = Signal(int)
            details_changed = Signal(str)
            finished = Signal(str, bool)
            failed = Signal(str, str)
            canceled = Signal()

            def __init__(self, media_path, options):
                super().__init__()
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

        workers_module.SubtitleGenerationWorker = SubtitleGenerationWorker
        sys.modules["services.SubtitleGenerationWorkers"] = workers_module

    if "services.SubtitleCudaRuntimeFlow" not in sys.modules:
        cuda_module = types.ModuleType("services.SubtitleCudaRuntimeFlow")

        class SubtitleCudaRuntimeFlow(QObject):
            status_changed = Signal(int, str)
            details_changed = Signal(int, str)
            finished = Signal(int)
            failed = Signal(int, str)
            canceled = Signal(int)
            thread_finished = Signal(int)

            def __init__(self, parent, ui):
                super().__init__(parent)
                self.ui = ui
                self._active = False
                self.cancel_calls = 0
                self.request_stop_calls = []

            def start(self, _run_id, _missing_packages):
                self._active = True
                return True

            def is_active(self):
                return self._active

            def cancel(self):
                self.cancel_calls += 1

            def request_stop(self, force=False):
                self.request_stop_calls.append(force)

        cuda_module.SubtitleCudaRuntimeFlow = SubtitleCudaRuntimeFlow
        sys.modules["services.SubtitleCudaRuntimeFlow"] = cuda_module

    if "services.SubtitleMaker" not in sys.modules:
        maker_module = types.ModuleType("services.SubtitleMaker")

        def get_missing_windows_cuda_runtime_packages():
            return []

        maker_module.get_missing_windows_cuda_runtime_packages = get_missing_windows_cuda_runtime_packages
        sys.modules["services.SubtitleMaker"] = maker_module


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
