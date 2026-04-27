import logging
import os
from pathlib import Path
import site


logger = logging.getLogger(__name__)


WINDOWS_CUDA_RUNTIME_PACKAGE_FILES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("nvidia-cublas-cu12", ("nvidia/cublas/bin/cublas64_12.dll",)),
    ("nvidia-cudnn-cu12", ("nvidia/cudnn/bin/cudnn64_9.dll",)),
    ("nvidia-cuda-nvrtc-cu12", ("nvidia/cuda_nvrtc/bin/nvrtc64_120_0.dll",)),
)


def _get_site_package_roots() -> list[Path]:
    candidate_roots: list[Path] = []
    try:
        candidate_roots.append(Path(site.getusersitepackages()))
    except (AttributeError, OSError, TypeError):
        logger.debug("Unable to resolve user site-packages path", exc_info=True)

    try:
        candidate_roots.extend(Path(path) for path in site.getsitepackages())
    except (AttributeError, OSError, TypeError):
        logger.debug("Unable to resolve global site-packages paths", exc_info=True)

    return candidate_roots


def get_missing_windows_cuda_runtime_packages() -> list[str]:
    if os.name != "nt":
        return []

    candidate_roots = _get_site_package_roots()
    missing_packages: list[str] = []

    for package_name, relative_paths in WINDOWS_CUDA_RUNTIME_PACKAGE_FILES:
        package_found = any((root / relative_path).is_file() for root in candidate_roots for relative_path in relative_paths)
        if not package_found:
            missing_packages.append(package_name)

    return missing_packages


def configure_windows_nvidia_runtime_paths():
    if os.name != "nt":
        return

    candidate_roots = _get_site_package_roots()

    dll_dirs: list[Path] = []
    for root in candidate_roots:
        for relative in {str(Path(relative_path).parent).replace("\\", "/") for _, relative_paths in WINDOWS_CUDA_RUNTIME_PACKAGE_FILES for relative_path in relative_paths}:
            dll_dir = root / relative
            if dll_dir.is_dir():
                dll_dirs.append(dll_dir)

    if not dll_dirs:
        return

    current_path_parts = os.environ.get("PATH", "").split(os.pathsep)
    normalized_existing = {str(Path(path).resolve()) for path in current_path_parts if path}

    for dll_dir in dll_dirs:
        resolved = str(dll_dir.resolve())
        if resolved not in normalized_existing:
            os.environ["PATH"] = resolved + os.pathsep + os.environ.get("PATH", "")
            normalized_existing.add(resolved)
        try:
            os.add_dll_directory(resolved)
        except (AttributeError, FileNotFoundError, OSError):
            logger.debug("Unable to register CUDA DLL directory | path=%s", resolved, exc_info=True)
