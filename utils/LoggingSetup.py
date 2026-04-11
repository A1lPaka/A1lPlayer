import logging
import os
import tempfile
from pathlib import Path


_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_CONFIGURED = False


def get_log_file_path() -> Path:
    if os.name == "nt":
        base_dir = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base_dir = Path(tempfile.gettempdir())
    return base_dir / "A1lPlayer" / "logs" / "app.log"


def configure_logging(level: int = logging.INFO) -> Path | None:
    global _CONFIGURED

    if _CONFIGURED:
        log_file_path = get_log_file_path()
        return log_file_path if log_file_path.exists() else None

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)
    handlers: list[logging.Handler] = []

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    handlers.append(console_handler)

    log_file_path: Path | None = None
    try:
        candidate = get_log_file_path()
        candidate.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(candidate, encoding="utf-8")
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
        log_file_path = candidate
    except OSError:
        log_file_path = None

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(level)
    for handler in handlers:
        root_logger.addHandler(handler)

    logging.captureWarnings(True)
    _CONFIGURED = True
    if log_file_path is None:
        logging.getLogger(__name__).warning("File logging is unavailable; continuing with console logging only")
    logging.getLogger(__name__).info("Logging configured%s", f" | file={log_file_path}" if log_file_path else "")
    return log_file_path
