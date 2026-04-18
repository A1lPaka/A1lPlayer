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


HELPER_SUBTITLE_GENERATION = "subtitle-generation"

EVENT_PROGRESS = "progress"


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


def build_finished_event(
    output_path: str = "",
    auto_open: bool = False,
    *,
    used_fallback_output_path: bool = False,
) -> dict:
    return {
        "event": EVENT_FINISHED,
        "output_path": str(output_path),
        "auto_open": bool(auto_open),
        "used_fallback_output_path": bool(used_fallback_output_path),
    }


def _coerce_optional_int(value) -> int | None:
    if value is None:
        return None
    return int(value)


def _coerce_optional_str(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
