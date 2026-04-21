import os
from pathlib import Path, PurePath
import sys


def res_path(relative_path: str) -> str:
    base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base_path, relative_path)


def canonical_path(path: str) -> str:
    try:
        return str(Path(path).expanduser().resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return os.path.abspath(os.path.normpath(os.path.expanduser(path)))


def normalize_path(path: str) -> str:
    return os.path.normcase(canonical_path(path))


def compact_path_for_display(path: str, max_chars: int = 80) -> str:
    text = str(path or "")
    max_chars = max(8, int(max_chars))
    if len(text) <= max_chars:
        return text

    parts = PurePath(text).parts
    if len(parts) >= 3:
        filename = parts[-1]
        separator = "\\" if "\\" in text else "/"
        if parts[0].endswith(("\\", "/")):
            prefix = f"{parts[0]}{parts[1]}"
        else:
            prefix = parts[0]
        compact = f"{prefix}{separator}...{separator}{filename}"
        if len(compact) <= max_chars:
            return compact

        keep = max(1, max_chars - len(prefix) - 3)
        return f"{prefix}...{filename[-keep:]}"

    return f"...{text[-(max_chars - 3):]}"
