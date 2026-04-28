import logging
import os
from pathlib import Path

from utils.runtime_assets import managed_cuda_runtime_root


logger = logging.getLogger(__name__)


WINDOWS_CUDA_RUNTIME_PACKAGE_FILES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "nvidia-cublas-cu12==12.9.2.10",
        (
            "nvidia/cublas/bin/cublas64_12.dll",
            "nvidia_cublas_cu12-12.9.2.10.dist-info/METADATA",
        ),
    ),
    (
        "nvidia-cudnn-cu12==9.20.0.48",
        (
            "nvidia/cudnn/bin/cudnn64_9.dll",
            "nvidia_cudnn_cu12-9.20.0.48.dist-info/METADATA",
        ),
    ),
    (
        "nvidia-cuda-nvrtc-cu12==12.9.86",
        (
            "nvidia/cuda_nvrtc/bin/nvrtc64_120_0.dll",
            "nvidia_cuda_nvrtc_cu12-12.9.86.dist-info/METADATA",
        ),
    ),
)


def _get_cuda_runtime_roots(runtime_roots: list[Path] | None = None) -> list[Path]:
    return runtime_roots or [managed_cuda_runtime_root()]


def get_missing_windows_cuda_runtime_packages(runtime_roots: list[Path] | None = None) -> list[str]:
    if os.name != "nt":
        return []

    candidate_roots = _get_cuda_runtime_roots(runtime_roots)
    missing_packages: list[str] = []

    for package_requirement, relative_paths in WINDOWS_CUDA_RUNTIME_PACKAGE_FILES:
        package_found = any(all((root / relative_path).is_file() for relative_path in relative_paths) for root in candidate_roots)
        if not package_found:
            missing_packages.append(package_requirement)

    return missing_packages


def configure_windows_nvidia_runtime_paths():
    if os.name != "nt":
        return

    candidate_roots = _get_cuda_runtime_roots()

    dll_dirs: list[Path] = []
    for root in candidate_roots:
        for relative in {
            str(Path(relative_path).parent).replace("\\", "/")
            for _, relative_paths in WINDOWS_CUDA_RUNTIME_PACKAGE_FILES
            for relative_path in relative_paths
            if Path(relative_path).suffix.lower() == ".dll"
        }:
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
