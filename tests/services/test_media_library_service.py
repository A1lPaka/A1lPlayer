from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtWidgets import QWidget

from services.MediaLibraryService import MediaLibraryService, SubtitleAttachResult

from tests.fakes import FakeMediaStore, FakePlayerWindow


class _FakeDragEnterEvent:
    def __init__(self, mime_data: QMimeData):
        self._mime_data = mime_data

    def mimeData(self):
        return self._mime_data


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


def test_open_recent_media_uses_normal_open_flow_without_prevalidation(monkeypatch):
    player = FakePlayerWindow()
    store = FakeMediaStore()
    service = MediaLibraryService(QWidget(), player, store)
    missing_path = r"Z:\offline\missing.mp4"
    open_calls = []

    def fake_open_media_paths(paths: list[str]) -> bool:
        open_calls.append(list(paths))
        return False

    monkeypatch.setattr(service, "open_media_paths", fake_open_media_paths)

    assert service.open_recent_media(missing_path) is False
    assert open_calls == [[missing_path]]


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


def test_media_finished_clears_saved_position_and_stops_autosave(workspace_tmp_path):
    player = FakePlayerWindow()
    store = FakeMediaStore()
    service = MediaLibraryService(QWidget(), player, store)
    media_path = str(workspace_tmp_path / "finished.mp4")
    (workspace_tmp_path / "finished.mp4").write_text("media")

    player.playback._has_media_loaded = True
    player.playback.media_confirmed.emit(101, media_path)

    assert service._session_autosave_timer.isActive() is True

    player.playback.media_finished.emit(media_path)

    assert store.cleared_positions == [media_path]
    assert service._session_autosave_timer.isActive() is False


def test_drag_enter_accepts_supported_local_media_without_full_classification(monkeypatch, workspace_tmp_path):
    player = FakePlayerWindow()
    store = FakeMediaStore()
    service = MediaLibraryService(QWidget(), player, store)
    media_path = workspace_tmp_path / "movie.mp4"
    media_path.write_text("media")

    monkeypatch.setattr(
        service._paths,
        "classify_drop_paths",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("drag-enter must stay cheap")),
    )

    mime_data = QMimeData()
    mime_data.setUrls([QUrl.fromLocalFile(str(media_path))])

    assert service.can_accept_drag_event(_FakeDragEnterEvent(mime_data)) is True


def test_drag_enter_accepts_local_directory_without_full_scan(monkeypatch, workspace_tmp_path):
    player = FakePlayerWindow()
    store = FakeMediaStore()
    service = MediaLibraryService(QWidget(), player, store)
    media_dir = workspace_tmp_path / "library"
    media_dir.mkdir()

    collect_calls = 0

    def fail_on_collect(_path):
        nonlocal collect_calls
        collect_calls += 1
        raise AssertionError("drag-enter must not scan directories")

    monkeypatch.setattr(service._paths, "collect_media_files", fail_on_collect)
    monkeypatch.setattr(
        service._paths,
        "classify_drop_paths",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("drag-enter must not fully classify")),
    )

    mime_data = QMimeData()
    mime_data.setUrls([QUrl.fromLocalFile(str(media_dir))])

    assert service.can_accept_drag_event(_FakeDragEnterEvent(mime_data)) is True
    assert collect_calls == 0


def test_drag_enter_rejects_unsupported_local_file(workspace_tmp_path):
    player = FakePlayerWindow()
    store = FakeMediaStore()
    service = MediaLibraryService(QWidget(), player, store)
    text_path = workspace_tmp_path / "notes.txt"
    text_path.write_text("hello")

    mime_data = QMimeData()
    mime_data.setUrls([QUrl.fromLocalFile(str(text_path))])

    assert service.can_accept_drag_event(_FakeDragEnterEvent(mime_data)) is False


def test_drag_enter_rejects_non_local_urls():
    player = FakePlayerWindow()
    store = FakeMediaStore()
    service = MediaLibraryService(QWidget(), player, store)

    mime_data = QMimeData()
    mime_data.setUrls([QUrl("https://example.com/video.mp4")])

    assert service.can_accept_drag_event(_FakeDragEnterEvent(mime_data)) is False


def test_drop_still_scans_directory_and_opens_media(workspace_tmp_path):
    player = FakePlayerWindow()
    store = FakeMediaStore()
    service = MediaLibraryService(QWidget(), player, store)
    media_dir = workspace_tmp_path / "library"
    media_dir.mkdir()
    first_media = media_dir / "a.mp4"
    second_media = media_dir / "b.mkv"
    subtitle = media_dir / "sub.srt"
    first_media.write_text("a")
    second_media.write_text("b")
    subtitle.write_text("sub")

    assert service.open_dropped_paths([str(media_dir)]) is True
    assert player.playback.last_open_paths["file_paths"] == [str(first_media), str(second_media)]


def test_attach_subtitle_unifies_manual_failure_ui(monkeypatch):
    player = FakePlayerWindow()
    store = FakeMediaStore()
    service = MediaLibraryService(QWidget(), player, store)
    player.playback._media_path = "C:/media/movie.mkv"
    player.playback.open_subtitle_result = False
    failure_calls = []

    monkeypatch.setattr("services.MediaLibraryService.show_open_subtitle_failed", lambda _parent: failure_calls.append(True))

    result = service.attach_subtitle(
        "C:/subs/manual.srt",
        source="manual",
        save_last_dir=True,
        show_failure_ui=True,
    )

    assert result == SubtitleAttachResult.LOAD_FAILED
    assert player.playback.opened_subtitles == ["C:/subs/manual.srt"]
    assert store.saved_last_open_dir == ["C:/subs/manual.srt"]
    assert failure_calls == [True]


def test_attach_subtitle_unifies_drop_flow(workspace_tmp_path):
    player = FakePlayerWindow()
    store = FakeMediaStore()
    service = MediaLibraryService(QWidget(), player, store)
    player.playback._media_path = "C:/media/movie.mkv"
    player.playback._has_media_loaded = True
    subtitle_path = workspace_tmp_path / "dropped.srt"
    subtitle_path.write_text("1")

    assert service.open_dropped_paths([str(subtitle_path)]) is True
    assert player.playback.opened_subtitles == [str(subtitle_path)]
    assert store.saved_last_open_dir == [str(subtitle_path)]


def test_attach_subtitle_reports_context_change_without_touching_vlc():
    player = FakePlayerWindow()
    store = FakeMediaStore()
    service = MediaLibraryService(QWidget(), player, store)
    player.playback._media_path = "C:/media/other.mkv"
    player.playback._request_id = 11

    result = service.attach_subtitle(
        "C:/subs/generated.srt",
        source="generated",
        save_last_dir=True,
        guard_media_path="C:/media/movie.mkv",
        guard_request_id=7,
    )

    assert result == SubtitleAttachResult.CONTEXT_CHANGED
    assert player.playback.opened_subtitles == []
    assert store.saved_last_open_dir == ["C:/subs/generated.srt"]
