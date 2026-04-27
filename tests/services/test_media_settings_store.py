import json

from services.app.MediaSettingsStore import MediaSettingsStore


class _FakeSettings:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.value_calls = []
        self.set_calls = []

    def value(self, key, default, type=str):
        self.value_calls.append((key, default, type))
        return self.values.get(key, default)

    def setValue(self, key, value):
        self.set_calls.append((key, value))
        self.values[key] = value


def test_get_recent_media_returns_raw_history_without_validation(monkeypatch):
    store = MediaSettingsStore(settings=None)
    raw_paths = [
        r"Z:\offline\movie.mp4",
        r"\\server\share\missing.mkv",
    ]
    set_calls = []

    monkeypatch.setattr(store, "_get_recent_media_paths", lambda: list(raw_paths))
    monkeypatch.setattr(store, "_set_recent_media_paths", lambda paths: set_calls.append(list(paths)))

    assert store.get_recent_media() == raw_paths
    assert set_calls == []


def test_get_saved_position_uses_cached_session_positions():
    settings = _FakeSettings({
        MediaSettingsStore._SESSION_POSITIONS_KEY: json.dumps({
            r"C:\Media\Movie.mkv": 4500,
        }),
    })
    store = MediaSettingsStore(settings=settings)

    assert store.get_saved_position(r"c:\media\movie.mkv") == 4500
    assert store.get_saved_position(r"C:\Media\Movie.mkv") == 4500

    assert [call[0] for call in settings.value_calls].count(
        MediaSettingsStore._SESSION_POSITIONS_KEY
    ) == 1


def test_save_position_updates_session_position_cache():
    settings = _FakeSettings({
        MediaSettingsStore._SESSION_POSITIONS_KEY: json.dumps({
            r"C:\Media\Old.mkv": 1000,
        }),
    })
    store = MediaSettingsStore(settings=settings)

    assert store.get_saved_position(r"C:\Media\Old.mkv") == 1000
    store.save_position(r"C:\Media\New.mkv", 2000, 10000)

    assert store.get_saved_position(r"C:\Media\New.mkv") == 2000
    assert [call[0] for call in settings.value_calls].count(
        MediaSettingsStore._SESSION_POSITIONS_KEY
    ) == 1


def test_save_position_stores_normalized_session_path(monkeypatch):
    monkeypatch.setattr(
        "services.MediaSettingsStore.normalize_path",
        lambda path: path.lower(),
    )
    monkeypatch.setattr(
        "services.MediaSettingsStore.canonical_path",
        lambda path: path,
    )
    settings = _FakeSettings({
        MediaSettingsStore._SESSION_POSITIONS_KEY: json.dumps({
            r"C:\Media\Movie.mkv": 1000,
        }),
    })
    store = MediaSettingsStore(settings=settings)

    store.save_position(r"c:\media\MOVIE.mkv", 2000, 10000)

    assert json.loads(settings.values[MediaSettingsStore._SESSION_POSITIONS_KEY]) == {
        r"c:\media\MOVIE.mkv": 2000,
    }


def test_clear_saved_position_updates_session_position_cache():
    settings = _FakeSettings({
        MediaSettingsStore._SESSION_POSITIONS_KEY: json.dumps({
            r"C:\Media\Movie.mkv": 4500,
        }),
    })
    store = MediaSettingsStore(settings=settings)

    assert store.get_saved_position(r"C:\Media\Movie.mkv") == 4500
    store.clear_saved_position(r"c:\media\movie.mkv")

    assert store.get_saved_position(r"C:\Media\Movie.mkv") == 0
    assert json.loads(settings.values[MediaSettingsStore._SESSION_POSITIONS_KEY]) == {}


def test_add_recent_path_stores_normalized_paths(monkeypatch):
    monkeypatch.setattr(
        "services.MediaSettingsStore.normalize_path",
        lambda path: path.lower(),
    )
    monkeypatch.setattr(
        "services.MediaSettingsStore.canonical_path",
        lambda path: path,
    )
    settings = _FakeSettings({
        MediaSettingsStore._RECENT_MEDIA_KEY: json.dumps([
            r"C:\Media\Movie.mkv",
            r"D:\Media\Other.mkv",
        ]),
    })
    store = MediaSettingsStore(settings=settings)

    store.add_recent_path(r"c:\media\MOVIE.mkv")

    assert json.loads(settings.values[MediaSettingsStore._RECENT_MEDIA_KEY]) == [
        r"c:\media\MOVIE.mkv",
        r"D:\Media\Other.mkv",
    ]


def test_save_last_open_dir_stores_normalized_directory(monkeypatch):
    monkeypatch.setattr("services.MediaSettingsStore.canonical_path", lambda path: path)
    settings = _FakeSettings()
    store = MediaSettingsStore(settings=settings)

    store.save_last_open_dir("C:/Media/Movie.mkv")

    assert settings.values[MediaSettingsStore._LAST_OPEN_DIR_KEY] == "C:/Media"
