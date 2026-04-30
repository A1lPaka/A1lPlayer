from __future__ import annotations

from contextlib import contextmanager
import logging
import os
from pathlib import Path
from typing import Iterator, TextIO


logger = logging.getLogger(__name__)


class RuntimeInstallLockError(RuntimeError):
    pass


@contextmanager
def runtime_install_lock(target: Path, component_name: str) -> Iterator[None]:
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(f"{target.name}.install.lock")
    lock_file = lock_path.open("a+", encoding="utf-8")
    try:
        if not _try_lock_file(lock_file):
            raise RuntimeInstallLockError(
                f"Another {component_name} installation is already running for:\n{target}"
            )
        yield
    finally:
        try:
            _unlock_file(lock_file)
        finally:
            lock_file.close()
            _remove_lock_file(lock_path)


def _try_lock_file(lock_file: TextIO) -> bool:
    lock_file.seek(0)
    lock_file.write("lock")
    lock_file.flush()
    lock_file.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock_file(lock_file: TextIO) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _remove_lock_file(lock_path: Path) -> None:
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        logger.debug("Failed to remove runtime install lock file | path=%s", lock_path, exc_info=True)
