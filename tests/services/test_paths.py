from utils.paths import compact_path_for_display


def test_compact_path_for_display_preserves_filename_extension_when_prefix_does_not_fit():
    path = r"C:\Users\danii\Videos\VeryLongMovieName.mp4"

    assert compact_path_for_display(path, max_chars=16) == "...MovieName.mp4"


def test_compact_path_for_display_prefers_filename_over_long_prefix():
    path = r"C:\Users\danii\Videos\movie.mp4"

    assert compact_path_for_display(path, max_chars=12) == "movie.mp4"
