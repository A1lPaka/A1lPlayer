from PySide6.QtWidgets import QWidget

from services.MediaLibraryService import MediaLibraryService

from tests.fakes import FakeMediaStore, FakePlayerWindow


def test_recent_media_commits_only_after_confirmed_media(monkeypatch, workspace_tmp_path):
    player = FakePlayerWindow()
    store = FakeMediaStore()
    service = MediaLibraryService(QWidget(), player, store)
    media_path = str(workspace_tmp_path / "movie.mp4")
    (workspace_tmp_path / "movie.mp4").write_text("media")

    monkeypatch.setattr(service._paths, "deduplicate_paths", lambda paths: list(paths))

    assert service.open_media_paths([media_path]) is True
    assert store.recent_paths == []

    player.playback.media_confirmed.emit(player.playback.current_request_id(), media_path)

    assert store.recent_paths == [media_path]
    assert store.saved_last_open_dir == [media_path]


def test_failed_open_does_not_commit_recent_media(monkeypatch, workspace_tmp_path):
    player = FakePlayerWindow()
    store = FakeMediaStore()
    service = MediaLibraryService(QWidget(), player, store)
    media_path = str(workspace_tmp_path / "broken.mp4")
    (workspace_tmp_path / "broken.mp4").write_text("media")

    monkeypatch.setattr(service._paths, "deduplicate_paths", lambda paths: list(paths))

    assert service.open_media_paths([media_path]) is True

    player.playback.playback_error.emit(player.playback.current_request_id(), media_path, "failed")

    assert store.recent_paths == []
    assert store.saved_positions == []


def test_save_and_restore_session_semantics(monkeypatch, workspace_tmp_path):
    player = FakePlayerWindow()
    store = FakeMediaStore()
    service = MediaLibraryService(QWidget(), player, store)
    media_path = str(workspace_tmp_path / "resume.mp4")
    (workspace_tmp_path / "resume.mp4").write_text("media")

    player.playback._session_snapshot = {
        "path": media_path,
        "position_ms": 3210,
        "total_ms": 10000,
    }
    player.playback._has_media_loaded = True
    monkeypatch.setattr(service._paths, "deduplicate_paths", lambda paths: list(paths))
    monkeypatch.setattr("services.MediaLibraryService.confirm_resume_playback", lambda *_args: True)

    service.save_time_session()

    assert store.saved_positions == [(media_path, 3210, 10000)]

    store.saved_position_lookup[media_path] = 3210
    player.playback._session_snapshot = None
    player.playback._has_media_loaded = False

    assert service.open_media_paths([media_path]) is True
    assert player.playback.last_open_paths["start_position_ms"] == 3210
