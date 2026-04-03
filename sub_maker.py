from dataclasses import dataclass
from pathlib import Path
from faster_whisper import WhisperModel

@dataclass
class SubtitleSegment:
    start: float
    end: float
    text: str


class SubtitleMaker:
    def __init__(self, model_size: str = "small", device: str = "cpu"):
        self.model_size = model_size
        self.device = device
        self._model = None

    def load_model(self):
        pass

    def transcribe_file(self, media_path: str, audio_track: int | None = None) -> list[SubtitleSegment]:
        pass

    def save_srt(self, segments: list[SubtitleSegment], output_path: str):
        pass