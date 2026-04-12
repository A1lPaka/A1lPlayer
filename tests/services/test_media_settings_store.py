from services.MediaSettingsStore import MediaSettingsStore


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
