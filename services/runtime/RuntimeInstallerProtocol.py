from __future__ import annotations

from dataclasses import asdict, dataclass
import json

from services.runtime.RuntimeExecution import (
    EVENT_CANCELED,
    EVENT_FAILED,
    EVENT_FINISHED,
    build_canceled_event,
    build_failed_event,
)


INSTALLER_CUDA_RUNTIME = "cuda-runtime"
INSTALLER_WHISPER_MODEL = "whisper-model"

EVENT_STATUS = "status"


@dataclass(frozen=True)
class CudaRuntimeInstallRequest:
    packages: tuple[str, ...]
    install_target: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, payload: str) -> "CudaRuntimeInstallRequest":
        data = json.loads(payload)
        packages = tuple(str(item).strip() for item in data.get("packages") or [] if str(item).strip())
        return cls(
            packages=packages,
            install_target=str(data["install_target"]),
        )


@dataclass(frozen=True)
class WhisperModelInstallRequest:
    model_size: str
    install_target: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, payload: str) -> "WhisperModelInstallRequest":
        data = json.loads(payload)
        return cls(
            model_size=str(data["model_size"]).strip(),
            install_target=str(data["install_target"]),
        )


def build_status_event(status: str, details: str = "") -> dict:
    return {
        "event": EVENT_STATUS,
        "status": str(status),
        "details": str(details or ""),
    }


def build_finished_event() -> dict:
    return {"event": EVENT_FINISHED}
