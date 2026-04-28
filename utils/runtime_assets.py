from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys


_APP_RUNTIME_ENV = "A1LPLAYER_RUNTIME_DIR"
_WRITABLE_RUNTIME_ENV = "A1LPLAYER_WRITABLE_RUNTIME_DIR"
_MODEL_ROOT_ENV = "A1LPLAYER_MODEL_ROOT"
_CUDA_TARGET_ENV = "A1LPLAYER_CUDA_TARGET"


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def runtime_root() -> Path:
    configured_root = os.environ.get(_APP_RUNTIME_ENV, "").strip()
    if configured_root:
        return Path(configured_root).expanduser().resolve()
    return app_root() / "runtime"


def writable_runtime_root() -> Path:
    configured_root = os.environ.get(_WRITABLE_RUNTIME_ENV, "").strip()
    if configured_root:
        return Path(configured_root).expanduser().resolve()
    if getattr(sys, "frozen", False) and os.name == "nt":
        program_data = os.environ.get("ProgramData", "").strip()
        if program_data:
            return Path(program_data) / "A1lPlayer" / "runtime"
    return runtime_root()


def model_root() -> Path:
    configured_root = os.environ.get(_MODEL_ROOT_ENV, "").strip()
    if configured_root:
        return Path(configured_root).expanduser().resolve()
    return runtime_root() / "models"


def managed_cuda_runtime_root() -> Path:
    configured_root = os.environ.get(_CUDA_TARGET_ENV, "").strip()
    if configured_root:
        return Path(configured_root).expanduser().resolve()
    return writable_runtime_root() / "components" / "cuda"


def configure_bundled_runtime_paths():
    root = runtime_root()
    vlc_root = root / "vlc"
    ffmpeg_bin = root / "ffmpeg" / "bin"
    cuda_root = managed_cuda_runtime_root()
    huggingface_root = writable_runtime_root() / "huggingface"

    _prepend_path_if_dir(vlc_root)
    _prepend_path_if_dir(ffmpeg_bin)

    vlc_plugins = vlc_root / "plugins"
    if vlc_plugins.is_dir():
        os.environ.setdefault("VLC_PLUGIN_PATH", str(vlc_plugins))

    os.environ.setdefault("HF_HOME", str(huggingface_root))
    os.environ.setdefault("HF_HUB_CACHE", str(huggingface_root / "hub"))

    if cuda_root.is_dir():
        cuda_root_text = str(cuda_root)
        if cuda_root_text not in sys.path:
            sys.path.insert(0, cuda_root_text)


def resolve_runtime_executable(name: str) -> str:
    configure_bundled_runtime_paths()
    executable_name = name if name.lower().endswith(".exe") else f"{name}.exe"
    bundled_executable = runtime_root() / "ffmpeg" / "bin" / executable_name
    if bundled_executable.is_file():
        return str(bundled_executable)
    return shutil.which(executable_name) or shutil.which(name) or name


def resolve_whisper_model_reference(model_size: str) -> str:
    configure_bundled_runtime_paths()
    normalized = str(model_size or "").strip()
    if not normalized:
        return "medium"

    root = model_root()
    writable_model_root = writable_runtime_root() / "models"
    candidates = (
        root / f"faster-whisper-{normalized}",
        root / normalized,
        writable_model_root / f"faster-whisper-{normalized}",
        writable_model_root / normalized,
    )
    for candidate in candidates:
        if candidate.is_dir():
            return str(candidate)
    return normalized


def _prepend_path_if_dir(path: Path):
    if not path.is_dir():
        return

    resolved = str(path.resolve())
    current_parts = [part for part in os.environ.get("PATH", "").split(os.pathsep) if part]
    normalized_parts = {str(Path(part).resolve()) for part in current_parts}
    if resolved not in normalized_parts:
        os.environ["PATH"] = resolved + os.pathsep + os.environ.get("PATH", "")

    try:
        os.add_dll_directory(resolved)
    except (AttributeError, FileNotFoundError, OSError):
        pass
