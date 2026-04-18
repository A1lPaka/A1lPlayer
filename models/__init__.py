from dataclasses import dataclass


# UI-agnostic payload for subtitle generation requests. It lives in models so
# services can consume dialog output without importing the Qt dialog module.
@dataclass
class SubtitleGenerationDialogResult:
    audio_stream_index: int | None
    audio_language: str | None
    device: str | None
    model_size: str
    output_format: str
    output_path: str
    auto_open_after_generation: bool
