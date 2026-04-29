from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import shutil
import threading

from services.runtime.RuntimeExecution import get_runtime_mode_label
from services.runtime.RuntimeInstallLock import runtime_install_lock
from services.runtime.RuntimeInstallerProtocol import (
    WhisperModelInstallRequest,
    build_failed_event,
    build_finished_event,
    build_status_event,
)
from utils.runtime_assets import (
    configure_bundled_runtime_paths,
    is_valid_whisper_model_dir,
    normalize_whisper_model_size,
    whisper_model_install_target,
)


logger = logging.getLogger(__name__)


class WhisperModelInstallCanceledError(RuntimeError):
    pass


@dataclass(frozen=True)
class WhisperModelInstallSource:
    mode: str
    repo_id: str
    revision: str
    location: str


WHISPER_MODEL_REVISIONS = {
    "tiny": "d90ca5f",
    "base": "ebe41f7",
    "small": "536b066",
    "medium": "08e178d",
    "large-v3": "edaa852",
}


def resolve_whisper_model_install_target(model_size: str) -> Path:
    target = whisper_model_install_target(model_size)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def resolve_whisper_model_install_source(model_size: str) -> WhisperModelInstallSource:
    normalized = normalize_whisper_model_size(model_size)
    repo_id = f"Systran/faster-whisper-{normalized}"
    revision = WHISPER_MODEL_REVISIONS.get(normalized, "main")
    return WhisperModelInstallSource(
        mode="huggingface-snapshot",
        repo_id=repo_id,
        revision=revision,
        location=f"https://huggingface.co/{repo_id}/tree/{revision}",
    )


def ensure_whisper_model_installed(
    request: WhisperModelInstallRequest,
    emit_event,
    cancel_event: threading.Event,
) -> None:
    configure_bundled_runtime_paths()
    model_size = normalize_whisper_model_size(request.model_size)
    install_target = Path(request.install_target)
    source = resolve_whisper_model_install_source(model_size)

    logger.info(
        "Whisper model installer starting | runtime_mode=%s | model=%s | source=%s | target=%s",
        get_runtime_mode_label(),
        model_size,
        source.location,
        install_target,
    )
    _emit_status(emit_event, "Downloading Whisper model...", request, source)

    if cancel_event.is_set():
        raise WhisperModelInstallCanceledError("Installation canceled before launch.")

    with runtime_install_lock(install_target, f"Whisper model '{model_size}'"):
        if is_valid_whisper_model_dir(install_target):
            _emit_status(emit_event, "Whisper model already installed.", request, source)
            emit_event(build_finished_event())
            return

        temp_target = install_target.with_name(f"{install_target.name}.partial")
        if temp_target.exists():
            shutil.rmtree(temp_target)
        temp_target.mkdir(parents=True, exist_ok=True)

        try:
            _download_snapshot(source.repo_id, source.revision, temp_target)
            if cancel_event.is_set():
                raise WhisperModelInstallCanceledError("Installation canceled after download.")
            if not is_valid_whisper_model_dir(temp_target):
                raise RuntimeError(
                    "Downloaded snapshot is incomplete: model.bin was not found."
                )
            _replace_whisper_model_target(temp_target, install_target)
        except Exception:
            if temp_target.exists():
                shutil.rmtree(temp_target, ignore_errors=True)
            raise

    _emit_status(emit_event, "Whisper model installed.", request, source)
    emit_event(build_finished_event())
    logger.info(
        "Whisper model installer finished | model=%s | target=%s",
        model_size,
        install_target,
    )


def build_whisper_model_failure_event(exc: BaseException) -> dict:
    return build_failed_event(
        "Failed to install Whisper model.",
        f"{type(exc).__name__}: {exc}",
    )


def _replace_whisper_model_target(temp_target: Path, install_target: Path) -> None:
    backup_target = install_target.with_name(f"{install_target.name}.previous")
    if backup_target.exists():
        shutil.rmtree(backup_target)

    if install_target.exists():
        install_target.replace(backup_target)

    try:
        temp_target.replace(install_target)
    except Exception:
        if backup_target.exists() and not install_target.exists():
            backup_target.replace(install_target)
        raise

    if backup_target.exists():
        shutil.rmtree(backup_target, ignore_errors=True)


def _download_snapshot(repo_id: str, revision: str, target: Path) -> None:
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise RuntimeError(
            "huggingface_hub is not installed, so models cannot be downloaded."
        ) from exc

    snapshot_download(
        repo_id=repo_id,
        revision=revision,
        local_dir=str(target),
        local_dir_use_symlinks=False,
    )


def _emit_status(
    emit_event,
    status: str,
    request: WhisperModelInstallRequest,
    source: WhisperModelInstallSource,
) -> None:
    details = "\n".join(
        [
            f"Runtime mode: {get_runtime_mode_label()}",
            f"Model: {normalize_whisper_model_size(request.model_size)}",
            f"Install source: {source.mode}",
            f"Source location: {source.location}",
            f"Source revision: {source.revision}",
            f"Install target: {request.install_target}",
        ]
    )
    emit_event(build_status_event(status, details))
