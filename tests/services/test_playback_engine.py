import importlib.util
import builtins
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
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
        self.release_calls = 0
        self.options = []

    def event_manager(self):
        return self._event_manager

    def release(self):
        self.release_calls += 1

    def add_option(self, option):
        self.options.append(option)


class _FakePlayer:
    def __init__(self):
        self.media = None
        self.spu = -1
        self.spu_calls = []
        self.add_slave_results = []
        self.subtitle_file_results = []
        self.audio_track = -1
        self.audio_device = "__default__"

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

    def audio_get_track_description(self):
        return []

    def audio_get_track(self):
        return self.audio_track

    def audio_set_track(self, track_id):
        self.audio_track = int(track_id)
        return 0

    def audio_output_device_enum(self):
        return None

    def audio_output_device_get(self):
        return self.audio_device

    def audio_output_device_set(self, _module, device_id):
        self.audio_device = device_id or "__default__"

    def audio_set_channel(self, _channel):
        return 0

    def video_get_spu_description(self):
        return []

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


def test_real_playback_engine_loader_bypasses_global_stub(monkeypatch):
    module, _fake_instance = _load_real_playback_engine(monkeypatch)
    stub_module = sys.modules.get("services.playback.PlaybackEngine")

    assert module is not stub_module
    assert Path(module.__file__).name == "PlaybackEngine.py"


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


def test_previous_media_is_released_when_replaced(monkeypatch):
    module, fake_instance = _load_real_playback_engine(monkeypatch)
    service = module.PlaybackService()

    service.load_media("A.mp4")
    first_media = fake_instance.media[-1]
    service.load_media("B.mp4")

    assert first_media.release_calls == 1
    assert _FakeVlc.EventType.MediaStateChanged not in first_media.event_manager().callbacks


def test_load_media_applies_start_time_option_in_seconds(monkeypatch):
    module, fake_instance = _load_real_playback_engine(monkeypatch)
    service = module.PlaybackService()

    service.load_media("movie.mp4", start_position_ms=25_500)

    assert fake_instance.media[-1].options == [":start-time=25.5"]


@pytest.mark.parametrize("failure", ["media_new", "set_media"])
def test_load_media_vlc_exception_emits_playback_error(monkeypatch, failure):
    module, fake_instance = _load_real_playback_engine(monkeypatch)
    service = module.PlaybackService()
    errors = SignalRecorder()
    service.playback_error.connect(errors)

    if failure == "media_new":
        monkeypatch.setattr(fake_instance, "media_new", lambda _path: (_ for _ in ()).throw(OSError("boom")))
    else:
        monkeypatch.setattr(fake_instance.player, "set_media", lambda _media: (_ for _ in ()).throw(OSError("boom")))

    request_id = service.load_media("broken.mp4")
    service._flush_player_events_from_qt_thread()

    assert errors.calls == [
        (
            request_id,
            "broken.mp4",
            "Failed to open or play this media file. The file may be corrupted or unsupported.",
        )
    ]
    assert service._current_media is None
    assert service._current_media_event_manager is None
    if failure == "set_media":
        assert fake_instance.media[-1].release_calls == 1


def test_vlc_menu_queries_and_setters_return_safe_fallbacks(monkeypatch):
    module, fake_instance = _load_real_playback_engine(monkeypatch)
    service = module.PlaybackService()
    service.load_media("movie.mp4")
    player = fake_instance.player

    monkeypatch.setattr(player, "audio_get_track_description", lambda: (_ for _ in ()).throw(OSError("boom")))
    monkeypatch.setattr(player, "audio_get_track", lambda: (_ for _ in ()).throw(OSError("boom")))
    monkeypatch.setattr(player, "audio_set_track", lambda _track_id: (_ for _ in ()).throw(OSError("boom")))
    monkeypatch.setattr(player, "audio_output_device_enum", lambda: (_ for _ in ()).throw(OSError("boom")))
    monkeypatch.setattr(player, "video_get_spu_description", lambda: (_ for _ in ()).throw(OSError("boom")))
    monkeypatch.setattr(player, "video_get_spu", lambda: (_ for _ in ()).throw(OSError("boom")))
    monkeypatch.setattr(player, "video_set_spu", lambda _track_id: (_ for _ in ()).throw(OSError("boom")))

    assert service.get_audio_tracks() == []
    assert service.get_current_audio_track() == -1
    assert service.set_audio_track(1) is False
    assert service.get_audio_devices() == [("__default__", "Default Device")]
    assert service.get_subtitle_tracks() == []
    assert service.get_current_subtitle_track() == -1
    assert service.set_subtitle_track(1) is False


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


def test_subtitle_attach_vlc_exception_returns_false_and_cleans_failed_copy(monkeypatch, workspace_tmp_path):
    module, fake_instance = _load_real_playback_engine(monkeypatch)
    service = module.PlaybackService()
    service.load_media("movie.mp4")
    previous_runtime = workspace_tmp_path / "previous.srt"
    failed_runtime = workspace_tmp_path / "failed-runtime.srt"
    previous_runtime.write_text("previous", encoding="utf-8")
    failed_runtime.write_text("failed", encoding="utf-8")
    service._runtime_subtitle_copy_path = str(previous_runtime)
    cleanup_callbacks = []

    monkeypatch.setattr(service, "_prepare_runtime_subtitle_copy", lambda _path: str(failed_runtime))
    monkeypatch.setattr(module.QTimer, "singleShot", lambda _delay_ms, callback: cleanup_callbacks.append(callback))
    monkeypatch.setattr(fake_instance.player, "add_slave", lambda *_args: (_ for _ in ()).throw(OSError("boom")))
    monkeypatch.setattr(fake_instance.player, "video_set_subtitle_file", lambda _path: (_ for _ in ()).throw(OSError("boom")))

    assert service.open_subtitle_file(str(failed_runtime)) is False
    assert service._runtime_subtitle_copy_path == str(previous_runtime)
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
