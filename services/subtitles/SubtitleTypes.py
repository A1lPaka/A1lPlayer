from dataclasses import dataclass


class SubtitleGenerationCanceledError(RuntimeError):
    pass


class SubtitleGenerationEmptyResultError(RuntimeError):
    pass


@dataclass
class SubtitleSegment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class AudioStreamInfo:
    stream_index: int
    label: str
    is_default: bool = False
