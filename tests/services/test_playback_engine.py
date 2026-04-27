import importlib.util
import builtins
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication

from tests.fakes import SignalRecorder


class _FakeEventManager:
    def __init__(self):
        self.callbacks = {}

    def event_attach(self, event_type, callback, *args):
        self.callbacks[event_type] = (callback, args)
        return 0

    def event_detach(self, event_type):
        self.callbacks.pop(event_type, None)

class _FakeMedia:
    def __init__(self, path):
        self.path = path
        self._event_manager = _FakeEventManager()

    def event_manager(self):
        return self._event_manager


class _FakePlayer:
    def __init__(self):
        self.media = None
        self.spu = -1
        self.spu_calls = []
        self.add_slave_results = []
        self.subtitle_file_results = []

    def set_media(self, media):
        self.media = media

    def get_media(self):
        return self.media

    def video_get_spu(self):
        return self.spu

    def video_set_spu(self, track_id):
        self.spu = int(track_id)
        self.spu_calls.append(int(track_id))
        return 0

    def add_slave(self, *_args):
        result = self.add_slave_results.pop(0) if self.add_slave_results else 0
        if result == 0:
            self.spu = 1
        return result

    def video_set_subtitle_file(self, _path):
        return self.subtitle_file_results.pop(0) if self.subtitle_file_results else 0

    def release(self):
        return None


class _FakeInstance:
    def __init__(self):
        self.player = _FakePlayer()
        self.media = []

    def media_player_new(self):
        return self.player

    def media_new(self, path):
        media = _FakeMedia(path)
        self.media.append(media)
        return media

    def release(self):
        return None


class _FakeVlc:
    EventType = SimpleNamespace(MediaStateChanged="MediaStateChanged")
    State = SimpleNamespace(
        Playing=SimpleNamespace(value=3),
        Paused=SimpleNamespace(value=4),
        Stopped=SimpleNamespace(value=5),
        Ended=SimpleNamespace(value=6),
        Error=SimpleNamespace(value=7),
    )
    AudioOutputChannel = SimpleNamespace(
        Stereo=SimpleNamespace(value=1),
        RStereo=SimpleNamespace(value=2),
        Left=SimpleNamespace(value=3),
        Right=SimpleNamespace(value=4),
    )
    MediaSlaveType = SimpleNamespace(subtitle="subtitle")
    VLCException = Exception

    def __init__(self, instance):
        self._instance = instance

    def Instance(self):
        return self._instance


def _load_real_playback_engine(monkeypatch):
    module_path = Path(__file__).parents[2] / "services" / "playback" / "PlaybackEngine.py"
    spec = importlib.util.spec_from_file_location("real_playback_engine_for_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fake_instance = _FakeInstance()
    monkeypatch.setattr(module, "vlc", _FakeVlc(fake_instance))
    return module, fake_instance


def _load_playback_engine_without_vlc(monkeypatch):
    module_path = Path(__file__).parents[2] / "services" / "playback" / "PlaybackEngine.py"
    spec = importlib.util.spec_from_file_location("real_playback_engine_missing_vlc_test", module_path)
    module = importlib.util.module_from_spec(spec)
    original_import = builtins.__import__

    def import_without_vlc(name, *args, **kwargs):
        if name == "vlc":
            raise ImportError("python-vlc is not installed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_vlc)
    spec.loader.exec_module(module)
    return module


def test_module_import_survives_missing_vlc(monkeypatch):
    module = _load_playback_engine_without_vlc(monkeypatch)

    service = module.PlaybackService()
    errors = SignalRecorder()
    service.playback_error.connect(errors)

    request_id = service.load_media("missing-vlc.mp4")
    QCoreApplication.processEvents()

    assert module.vlc is None
    assert service.is_backend_available() is False
    assert errors.calls == [
        (
            request_id,
            "missing-vlc.mp4",
            module.PLAYBACK_BACKEND_UNAVAILABLE_MESSAGE,
        )
    ]


def test_late_media_error_is_ignored_after_new_media_load(monkeypatch):
    module, fake_instance = _load_real_playback_engine(monkeypatch)
    service = module.PlaybackService()
    errors = SignalRecorder()
    service.playback_error.connect(errors)

    first_request_id = service.load_media("A.mp4")
    first_media = fake_instance.media[-1]
    old_callback, old_args = first_media.event_manager().callbacks[_FakeVlc.EventType.MediaStateChanged]
    second_request_id = service.load_media("B.mp4")

    event = SimpleNamespace(u=SimpleNamespace(new_state=_FakeVlc.State.Error.value))
    old_callback(event, *old_args)
    service._flush_player_events_from_qt_thread()

    assert second_request_id != first_request_id
    assert errors.calls == []


def test_backend_creation_failure_emits_playback_error(monkeypatch):
    module, _fake_instance = _load_real_playback_engine(monkeypatch)

    class _BrokenVlc(_FakeVlc):
        def Instance(self):
            raise OSError("libVLC was not found")

    monkeypatch.setattr(module, "vlc", _BrokenVlc(_FakeInstance()))
    service = module.PlaybackService()
    errors = SignalRecorder()
    service.playback_error.connect(errors)

    request_id = service.load_media("missing-backend.mp4")
    QCoreApplication.processEvents()

    assert service.is_backend_available() is False
    assert request_id == 1
    assert errors.calls == [
        (
            request_id,
            "missing-backend.mp4",
            module.PLAYBACK_BACKEND_UNAVAILABLE_MESSAGE,
        )
    ]


def test_late_media_error_keeps_current_runtime_subtitle_copy(monkeypatch, workspace_tmp_path):
    module, fake_instance = _load_real_playback_engine(monkeypatch)
    service = module.PlaybackService()

    first_request_id = service.load_media("A.mp4")
    first_media = fake_instance.media[-1]
    old_callback, old_args = first_media.event_manager().callbacks[_FakeVlc.EventType.MediaStateChanged]
    second_request_id = service.load_media("B.mp4")
    runtime_copy = workspace_tmp_path / "current-runtime.srt"
    runtime_copy.write_text("subtitle", encoding="utf-8")
    service._runtime_subtitle_copy_path = str(runtime_copy)

    event = SimpleNamespace(u=SimpleNamespace(new_state=_FakeVlc.State.Error.value))
    old_callback(event, *old_args)
    service._flush_player_events_from_qt_thread()

    assert second_request_id != first_request_id
    assert service._runtime_subtitle_copy_path == str(runtime_copy)
    assert runtime_copy.is_file()


def test_failed_subtitle_load_restores_previous_runtime_track(monkeypatch, workspace_tmp_path):
    module, fake_instance = _load_real_playback_engine(monkeypatch)
    service = module.PlaybackService()
    service.load_media("movie.mp4")
    previous_runtime = workspace_tmp_path / "previous.srt"
    broken_subtitle = workspace_tmp_path / "broken.srt"
    previous_runtime.write_text("previous", encoding="utf-8")
    broken_subtitle.write_text("broken", encoding="utf-8")
    service._runtime_subtitle_copy_path = str(previous_runtime)
    service.player.spu = -1
    service.player.add_slave_results = [1, 1, 0]
    service.player.subtitle_file_results = [1, 1]
    monkeypatch.setattr(service, "_prepare_runtime_subtitle_copy", lambda _path: str(broken_subtitle))

    assert service.open_subtitle_file(str(broken_subtitle)) is False

    assert service.player.spu_calls[-1] == -1
    assert service.player.spu == -1
    assert service._runtime_subtitle_copy_path == str(previous_runtime)


def test_failed_subtitle_load_schedules_cleanup_for_failed_runtime_copy(monkeypatch, workspace_tmp_path):
    module, fake_instance = _load_real_playback_engine(monkeypatch)
    service = module.PlaybackService()
    service.load_media("movie.mp4")
    previous_runtime = workspace_tmp_path / "previous.srt"
    failed_runtime = workspace_tmp_path / "failed-runtime.srt"
    previous_runtime.write_text("previous", encoding="utf-8")
    failed_runtime.write_text("failed", encoding="utf-8")
    service._runtime_subtitle_copy_path = str(previous_runtime)
    service.player.add_slave_results = [1, 1, 0]
    service.player.subtitle_file_results = [1, 1]
    cleanup_callbacks = []

    monkeypatch.setattr(service, "_prepare_runtime_subtitle_copy", lambda _path: str(failed_runtime))
    monkeypatch.setattr(module.QTimer, "singleShot", lambda _delay_ms, callback: cleanup_callbacks.append(callback))

    assert service.open_subtitle_file(str(failed_runtime)) is False
    assert service._runtime_subtitle_copy_path == str(previous_runtime)
    assert failed_runtime.is_file()
    assert len(cleanup_callbacks) == 1

    cleanup_callbacks[0]()

    assert previous_runtime.is_file()
    assert failed_runtime.exists() is False


def test_queued_player_events_are_discarded_after_shutdown(monkeypatch):
    module, _fake_instance = _load_real_playback_engine(monkeypatch)
    service = module.PlaybackService()
    playing = SignalRecorder()
    service.playing.connect(playing)

    service._queued_player_events.append(("playing", 1, "A.mp4"))
    service._player_events_flush_scheduled = True
    service._is_shutdown = True

    service._flush_player_events_from_qt_thread()

    assert playing.calls == []
    assert list(service._queued_player_events) == []
    assert service._player_events_flush_scheduled is False


def test_video_geometry_probe_callback_stops_after_shutdown(monkeypatch):
    module, _fake_instance = _load_real_playback_engine(monkeypatch)
    service = module.PlaybackService()
    scheduled_callbacks = []
    probe_calls = []

    monkeypatch.setattr(module.QTimer, "singleShot", lambda _delay_ms, callback: scheduled_callbacks.append(callback))
    monkeypatch.setattr(service, "get_video_dimensions", lambda: None)

    original_schedule = service._schedule_video_geometry_probe

    def tracking_schedule(request_id, attempts=12, delay_ms=120):
        probe_calls.append((request_id, attempts, delay_ms))
        return original_schedule(request_id, attempts, delay_ms)

    monkeypatch.setattr(service, "_schedule_video_geometry_probe", tracking_schedule)

    service._current_request_id = 7
    service._schedule_video_geometry_probe(7, attempts=2, delay_ms=1)
    assert len(scheduled_callbacks) == 1

    service._is_shutdown = True
    scheduled_callbacks[0]()

    assert probe_calls == [(7, 2, 1)]
