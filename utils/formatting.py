import os


def format_ms(ms: int) -> str:
    total_seconds = max(0, ms // 1000)
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def format_speed(speed: float) -> str:
    return f"x{float(speed):.2f}"


def build_window_title(
    media_path: str | None = None,
    base_title: str = "A1lPlayer",
    max_media_title_length: int = 36,
) -> str:
    if not media_path:
        return base_title

    media_name = os.path.splitext(os.path.basename(media_path))[0].strip()
    if not media_name:
        return base_title

    if len(media_name) > max_media_title_length:
        media_name = f"{media_name[:max_media_title_length].rstrip()}..."

    return f"{base_title}: {media_name}"
