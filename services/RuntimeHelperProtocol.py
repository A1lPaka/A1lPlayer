from __future__ import annotations

from dataclasses import asdict, dataclass
import json


HELPER_SUBTITLE_GENERATION = "subtitle-generation"

EVENT_PROGRESS = "progress"
EVENT_FINISHED = "finished"
EVENT_FAILED = "failed"
EVENT_CANCELED = "canceled"


@dataclass(frozen=True)
class SubtitleGenerationRequest:
    media_path: str
    audio_stream_index: int | None
    audio_language: str | None
    device: str | None
    model_size: str
    output_format: str
    output_path: str
    auto_open_after_generation: bool

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, payload: str) -> "SubtitleGenerationRequest":
        data = json.loads(payload)
        return cls(
            media_path=str(data["media_path"]),
            audio_stream_index=_coerce_optional_int(data.get("audio_stream_index")),
            audio_language=_coerce_optional_str(data.get("audio_language")),
            device=_coerce_optional_str(data.get("device")),
            model_size=str(data["model_size"]),
            output_format=str(data["output_format"]),
            output_path=str(data["output_path"]),
            auto_open_after_generation=bool(data["auto_open_after_generation"]),
        )


def build_progress_event(status: str, progress: int, details: str) -> dict:
    return {
        "event": EVENT_PROGRESS,
        "status": str(status),
        "progress": int(progress),
        "details": str(details or ""),
    }


def build_finished_event(output_path: str = "", auto_open: bool = False) -> dict:
    return {
        "event": EVENT_FINISHED,
        "output_path": str(output_path),
        "auto_open": bool(auto_open),
    }


def build_failed_event(user_message: str, diagnostics: str | None = None) -> dict:
    return {
        "event": EVENT_FAILED,
        "user_message": str(user_message),
        "diagnostics": str(diagnostics or ""),
    }


def build_canceled_event() -> dict:
    return {"event": EVENT_CANCELED}


def _coerce_optional_int(value) -> int | None:
    if value is None:
        return None
    return int(value)


def _coerce_optional_str(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
