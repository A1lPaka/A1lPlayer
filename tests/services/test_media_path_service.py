from services.media.MediaPathService import (
    MEDIA_EXTENSIONS,
    SUBTITLE_EXTENSIONS,
    MediaPathService,
    build_file_dialog_filter,
)


def test_classifies_media_subtitle_and_unknown_files(workspace_tmp_path):
    service = MediaPathService()
    media_path = workspace_tmp_path / "movie.MP4"
    subtitle_path = workspace_tmp_path / "movie.SRT"
    unknown_path = workspace_tmp_path / "notes.txt"

    media_path.write_text("media")
    subtitle_path.write_text("subtitle")
    unknown_path.write_text("notes")

    result = service.classify_drop_paths([str(media_path), str(subtitle_path), str(unknown_path)])

    assert result == {
        "media_paths": [str(media_path)],
        "subtitle_paths": [str(subtitle_path)],
    }


def test_collect_media_files_sorts_by_basename_and_ignores_non_media(workspace_tmp_path):
    service = MediaPathService()
    folder = workspace_tmp_path / "library"
    folder.mkdir()
    second = folder / "b.mkv"
    first = folder / "A.mp4"
    ignored = folder / "subs.srt"

    second.write_text("b")
    first.write_text("a")
    ignored.write_text("subtitle")

    assert service.collect_media_files(str(folder)) == [str(first), str(second)]


def test_deduplicate_paths_treats_relative_and_absolute_paths_as_same(monkeypatch, workspace_tmp_path):
    service = MediaPathService()
    media_path = workspace_tmp_path / "movie.mp4"
    media_path.write_text("media")

    monkeypatch.chdir(workspace_tmp_path)

    assert service.deduplicate_paths(["movie.mp4", str(media_path)]) == ["movie.mp4"]


def test_file_dialog_filters_keep_expected_patterns():
    media_filter = build_file_dialog_filter("Media Files", MEDIA_EXTENSIONS)
    subtitle_filter = build_file_dialog_filter("Subtitle Files", SUBTITLE_EXTENSIONS)

    assert media_filter.startswith("Media Files (*.mp4 *.mkv *.avi")
    assert "*.aac" in media_filter
    assert media_filter.endswith(";;All Files (*)")
    assert subtitle_filter == "Subtitle Files (*.srt *.ass *.ssa *.sub *.vtt);;All Files (*)"
