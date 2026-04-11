from __future__ import annotations

from dataclasses import asdict, dataclass
import json


INSTALLER_CUDA_RUNTIME = "cuda-runtime"

EVENT_FINISHED = "finished"
EVENT_FAILED = "failed"
EVENT_CANCELED = "canceled"
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


def build_status_event(status: str, details: str = "") -> dict:
    return {
        "event": EVENT_STATUS,
        "status": str(status),
        "details": str(details or ""),
    }


def build_finished_event() -> dict:
    return {"event": EVENT_FINISHED}


def build_failed_event(user_message: str, diagnostics: str | None = None) -> dict:
    return {
        "event": EVENT_FAILED,
        "user_message": str(user_message),
        "diagnostics": str(diagnostics or ""),
    }


def build_canceled_event() -> dict:
    return {"event": EVENT_CANCELED}
