import os


def test_utils_facade_exports_existing_public_names():
    from utils import (
        BASE_HEIGHT,
        BASE_WIDTH,
        Metrics,
        build_window_title,
        color_from_state,
        format_ms,
        format_speed,
        get_metrics,
        normalize_path,
        res_path,
    )

    assert BASE_WIDTH == 1920
    assert BASE_HEIGHT == 1080
    assert Metrics.__name__ == "Metrics"
    assert callable(build_window_title)
    assert callable(color_from_state)
    assert callable(format_ms)
    assert callable(format_speed)
    assert callable(get_metrics)
    assert callable(normalize_path)
    assert callable(res_path)


def test_utils_formatting_outputs_are_unchanged():
    from utils import build_window_title, format_ms, format_speed

    assert format_ms(0) == "00:00"
    assert format_ms(3_661_000) == "01:01:01"
    assert format_speed(1.0) == "x1.00"
    assert build_window_title(None) == "A1lPlayer"
    assert build_window_title("C:/media/example movie.mkv") == "A1lPlayer: example movie"


def test_utils_path_and_theme_outputs_are_unchanged():
    from utils import color_from_state, normalize_path

    raw_path = "C:/media/../media/movie.mkv"

    assert normalize_path(raw_path) == os.path.normcase(os.path.normpath(raw_path))
    assert color_from_state("inactive", (100, 100, 100)) == (30, 30, 30)
