from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path

from PySide6.QtWidgets import QWidget

from ui.MessageBoxService import (
    confirm_overwrite_subtitle,
    show_audio_stream_inspection_failed,
    show_audio_stream_inspection_warning,
    show_audio_stream_no_longer_available,
    show_audio_streams_still_loading,
    show_choose_output_path_first,
    show_no_audio_streams_found,
    show_subtitle_output_path_unavailable,
)
from ui.SubtitleGenerationDialog import SubtitleGenerationDialogResult


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubtitleGenerationValidationResult:
    is_valid: bool


class AudioStreamProbeState(Enum):
    IDLE = auto()
    LOADING = auto()
    READY = auto()
    FAILED = auto()


class SubtitleGenerationPreflight:
    DEFAULT_GENERATION_AUDIO_TRACKS: tuple[tuple[int | None, str], ...] = ((None, "Current / default"),)

    def __init__(self, parent: QWidget):
        self._parent = parent

    def build_audio_track_choices(self, audio_streams) -> list[tuple[int | None, str]]:
        generated_tracks: list[tuple[int | None, str]] = list(self.DEFAULT_GENERATION_AUDIO_TRACKS)
        generated_tracks.extend((stream.stream_index, stream.label) for stream in audio_streams)
        return generated_tracks

    def validate_generation_request(
        self,
        media_path: str,
        options: SubtitleGenerationDialogResult,
        *,
        probe_state: AudioStreamProbeState,
        audio_streams=None,
        probe_error: str | None = None,
    ) -> SubtitleGenerationValidationResult:
        if not self._validate_output_path(options):
            return SubtitleGenerationValidationResult(is_valid=False)

        if not self._validate_audio_stream_selection(
            media_path,
            options,
            probe_state=probe_state,
            audio_streams=audio_streams,
            probe_error=probe_error,
        ):
            return SubtitleGenerationValidationResult(is_valid=False)

        return SubtitleGenerationValidationResult(is_valid=True)

    def format_audio_stream_probe_error(self, reason: str) -> str:
        normalized_reason = (reason or "").strip() or "Audio stream inspection failed."
        if normalized_reason.lower().startswith("ffprobe was not found"):
            return "ffprobe was not found. Please install ffmpeg/ffprobe to inspect audio streams."
        if normalized_reason.lower().startswith("audio stream inspection timed out"):
            return (
                f"{normalized_reason}\n\n"
                "The media file may be unavailable, the storage may be too slow, or ffprobe may have stopped responding."
            )
        return normalized_reason

    def _validate_output_path(self, options: SubtitleGenerationDialogResult) -> bool:
        output_path = options.output_path.strip()
        if not output_path:
            logger.info("Subtitle generation validation failed: empty output path")
            show_choose_output_path_first(self._parent)
            return False

        output_file = Path(output_path)
        preflight_error = self._preflight_subtitle_output_path(output_file)
        if preflight_error is not None:
            logger.warning(
                "Subtitle generation validation failed: output path preflight failed | output=%s | reason=%s",
                output_file,
                preflight_error,
            )
            show_subtitle_output_path_unavailable(
                self._parent,
                str(output_file),
                preflight_error,
            )
            return False

        if os.path.exists(output_path) and not confirm_overwrite_subtitle(self._parent, output_path):
            logger.info("Subtitle generation overwrite declined by user | output=%s", output_path)
            return False

        return True

    def _validate_audio_stream_selection(
        self,
        media_path: str,
        options: SubtitleGenerationDialogResult,
        *,
        probe_state: AudioStreamProbeState,
        audio_streams,
        probe_error: str | None,
    ) -> bool:
        if probe_state in (AudioStreamProbeState.IDLE, AudioStreamProbeState.LOADING):
            logger.info(
                "Subtitle generation blocked because audio streams are still loading | media=%s | probe_state=%s",
                media_path,
                probe_state.name.lower(),
            )
            show_audio_streams_still_loading(self._parent)
            return False

        if probe_state == AudioStreamProbeState.FAILED:
            reason = (probe_error or "").strip() or "Audio stream inspection failed."
            logger.warning(
                "Subtitle generation aborted because cached audio stream inspection failed during validation | media=%s",
                media_path,
            )
            show_audio_stream_inspection_failed(
                self._parent,
                self.format_audio_stream_probe_error(reason),
            )
            return False

        audio_streams = list(audio_streams or [])

        if not audio_streams:
            logger.warning("Subtitle generation aborted because media has no audio streams | media=%s", media_path)
            show_no_audio_streams_found(self._parent)
            return False

        if options.audio_stream_index is None:
            return True

        available_stream_indices = {stream.stream_index for stream in audio_streams}
        if options.audio_stream_index in available_stream_indices:
            return True

        logger.warning(
            "Subtitle generation aborted because selected audio stream is not available | media=%s | audio_stream_index=%s",
            media_path,
            options.audio_stream_index,
        )
        show_audio_stream_no_longer_available(self._parent)
        return False

    def _preflight_subtitle_output_path(self, output_path: Path) -> str | None:
        try:
            parent_dir = output_path.expanduser().resolve(strict=False).parent
        except (OSError, RuntimeError, ValueError) as exc:
            return f"Failed to resolve the destination folder: {exc}"

        if not parent_dir.name and not parent_dir.anchor:
            return "Failed to resolve the destination folder."

        if parent_dir.exists():
            if not parent_dir.is_dir():
                return "The destination folder path points to a file."
            if not os.access(parent_dir, os.W_OK):
                return "Failed to write to the destination folder: access is denied."
            return None

        if parent_dir.anchor and not Path(parent_dir.anchor).exists():
            return "Failed to resolve the destination drive or root folder."

        existing_parent = next((candidate for candidate in (parent_dir, *parent_dir.parents) if candidate.exists()), None)
        if existing_parent is None:
            return "Failed to resolve the destination folder."
        if not existing_parent.is_dir():
            return "The destination folder path points to a file."
        if not os.access(existing_parent, os.W_OK):
            return "Failed to create the destination folder: access is denied."

        return None
