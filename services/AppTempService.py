import logging
import shutil
import tempfile
import time
import uuid
from pathlib import Path


logger = logging.getLogger(__name__)


class AppTempService:
    _APP_DIR_NAME = "A1lPlayer"
    _RUNTIME_SUBTITLES_DIR = "runtime-subtitles"
    _SUBTITLE_GENERATION_DIR = "subtitle-generation"
    _STARTUP_CLEANUP_MAX_AGE_SECONDS = 24 * 60 * 60
    _startup_cleanup_ran = False

    @classmethod
    def get_app_temp_root(cls) -> Path:
        return Path(tempfile.gettempdir()) / cls._APP_DIR_NAME

    @classmethod
    def get_runtime_subtitles_dir(cls) -> Path:
        return cls.get_app_temp_root() / cls._RUNTIME_SUBTITLES_DIR

    @classmethod
    def get_subtitle_generation_dir(cls) -> Path:
        return cls.get_app_temp_root() / cls._SUBTITLE_GENERATION_DIR

    @classmethod
    def ensure_dir(cls, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        return path

    @classmethod
    def create_runtime_subtitle_copy_path(cls, source_path: str | Path) -> Path:
        source = Path(source_path)
        runtime_dir = cls.ensure_dir(cls.get_runtime_subtitles_dir())
        safe_suffix = source.suffix or ".srt"
        return runtime_dir / f"subtitle-{uuid.uuid4().hex}{safe_suffix}"

    @classmethod
    def create_subtitle_generation_file_path(cls, suffix: str, prefix: str = "artifact-") -> Path:
        generation_dir = cls.ensure_dir(cls.get_subtitle_generation_dir())
        safe_suffix = suffix if str(suffix).startswith(".") else f".{suffix}"
        return generation_dir / f"{prefix}{uuid.uuid4().hex}{safe_suffix}"

    @classmethod
    def cleanup_startup_orphans(cls):
        if cls._startup_cleanup_ran:
            return
        cls._startup_cleanup_ran = True

        app_root = cls.get_app_temp_root()
        if not app_root.exists():
            return

        now = time.time()
        for owned_dir in (cls.get_runtime_subtitles_dir(), cls.get_subtitle_generation_dir()):
            cls._cleanup_owned_dir(owned_dir, now)

        cls._remove_dir_if_empty(app_root)

    @classmethod
    def remove_file_if_exists(cls, path: str | Path, *, log_context: str = "temporary file cleanup"):
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            logger.debug("Best-effort %s failed | path=%s", log_context, path, exc_info=True)

    @classmethod
    def remove_dir_if_exists(cls, path: str | Path, *, log_context: str = "temporary directory cleanup"):
        try:
            shutil.rmtree(path, ignore_errors=False)
        except FileNotFoundError:
            return
        except OSError:
            logger.debug("Best-effort %s failed | path=%s", log_context, path, exc_info=True)

    @classmethod
    def _cleanup_owned_dir(cls, owned_dir: Path, now: float):
        if not owned_dir.exists():
            return

        try:
            children = list(owned_dir.iterdir())
        except OSError:
            logger.debug("Failed to enumerate app temp directory for cleanup | path=%s", owned_dir, exc_info=True)
            return

        for child in children:
            if not cls._is_stale(child, now):
                continue
            if child.is_dir():
                cls.remove_dir_if_exists(child, log_context="startup app temp directory cleanup")
            else:
                cls.remove_file_if_exists(child, log_context="startup app temp file cleanup")

        cls._remove_dir_if_empty(owned_dir)

    @classmethod
    def _is_stale(cls, path: Path, now: float) -> bool:
        try:
            modified_at = path.stat().st_mtime
        except OSError:
            logger.debug("Failed to stat app temp artifact for cleanup | path=%s", path, exc_info=True)
            return False
        return (now - modified_at) >= cls._STARTUP_CLEANUP_MAX_AGE_SECONDS

    @classmethod
    def _remove_dir_if_empty(cls, path: Path):
        try:
            path.rmdir()
        except FileNotFoundError:
            return
        except OSError:
            return
