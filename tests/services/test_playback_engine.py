import importlib.util
from pathlib import Path
from types import SimpleNamespace

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

    def set_media(self, media):
        self.media = media

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
    VLCException = Exception

    def __init__(self, instance):
        self._instance = instance

    def Instance(self):
        return self._instance


def _load_real_playback_engine(monkeypatch):
    module_path = Path(__file__).parents[2] / "services" / "PlaybackEngine.py"
    spec = importlib.util.spec_from_file_location("real_playback_engine_for_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fake_instance = _FakeInstance()
    monkeypatch.setattr(module, "vlc", _FakeVlc(fake_instance))
    return module, fake_instance


def test_late_media_error_keeps_original_request_id(monkeypatch):
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
    assert errors.calls == [
        (
            first_request_id,
            "A.mp4",
            "Failed to open or play this media file. The file may be corrupted or unsupported.",
        )
    ]
