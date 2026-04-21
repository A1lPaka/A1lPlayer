import os
from pathlib import Path
import sys


def res_path(relative_path: str) -> str:
    base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base_path, relative_path)


def normalize_path(path: str) -> str:
    try:
        return os.path.normcase(str(Path(path).expanduser().resolve(strict=False)))
    except (OSError, RuntimeError, ValueError):
        return os.path.normcase(os.path.abspath(os.path.normpath(os.path.expanduser(path))))
