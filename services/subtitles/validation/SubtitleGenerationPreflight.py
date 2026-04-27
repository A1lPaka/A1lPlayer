from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
import tempfile

from PySide6.QtWidgets import QWidget

from models.SubtitleGenerationDialogResult import SubtitleGenerationDialogResult


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubtitleGenerationValidationResult:
    is_valid: bool
    reason: "SubtitleGenerationValidationFailure | None" = None
    output_path: str | None = None
    preflight_error: str | None = None
    probe_error: str | None = None
    formatted_reason: str | None = None


class SubtitleGenerationValidationFailure(Enum):
    EMPTY_OUTPUT_PATH = auto()
    OUTPUT_PATH_UNAVAILABLE = auto()
    OVERWRITE_CONFIRMATION_REQUIRED = auto()
    AUDIO_STREAMS_STILL_LOADING = auto()
    AUDIO_STREAM_INSPECTION_FAILED = auto()
    NO_AUDIO_STREAMS_FOUND = auto()
    AUDIO_STREAM_NO_LONGER_AVAILABLE = auto()


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
        output_path_result = self._validate_output_path(options)
        if not output_path_result.is_valid:
            return output_path_result

        audio_stream_result = self._validate_audio_stream_selection(
            media_path,
            options,
            probe_state=probe_state,
            audio_streams=audio_streams,
            probe_error=probe_error,
        )
        if not audio_stream_result.is_valid:
            return audio_stream_result

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

    def _validate_output_path(self, options: SubtitleGenerationDialogResult) -> SubtitleGenerationValidationResult:
        output_path = options.output_path.strip()
        if not output_path:
            logger.info("Subtitle generation validation failed: empty output path")
            return SubtitleGenerationValidationResult(
                is_valid=False,
                reason=SubtitleGenerationValidationFailure.EMPTY_OUTPUT_PATH,
            )

        output_file = Path(output_path)
        preflight_error = self._preflight_subtitle_output_path(output_file)
        if preflight_error is not None:
            logger.warning(
                "Subtitle generation validation failed: output path preflight failed | output=%s | reason=%s",
                output_file,
                preflight_error,
            )
            return SubtitleGenerationValidationResult(
                is_valid=False,
                reason=SubtitleGenerationValidationFailure.OUTPUT_PATH_UNAVAILABLE,
                output_path=str(output_file),
                preflight_error=preflight_error,
            )

        if output_file.exists() and output_file.is_dir():
            return SubtitleGenerationValidationResult(
                is_valid=False,
                reason=SubtitleGenerationValidationFailure.OUTPUT_PATH_UNAVAILABLE,
                output_path=str(output_file),
                preflight_error="The destination output path points to a folder.",
            )

        if os.path.exists(output_path):
            return SubtitleGenerationValidationResult(
                is_valid=False,
                reason=SubtitleGenerationValidationFailure.OVERWRITE_CONFIRMATION_REQUIRED,
                output_path=output_path,
            )

        return SubtitleGenerationValidationResult(is_valid=True)

    def _validate_audio_stream_selection(
        self,
        media_path: str,
        options: SubtitleGenerationDialogResult,
        *,
        probe_state: AudioStreamProbeState,
        audio_streams,
        probe_error: str | None,
    ) -> SubtitleGenerationValidationResult:
        if probe_state in (AudioStreamProbeState.IDLE, AudioStreamProbeState.LOADING):
            logger.info(
                "Subtitle generation blocked because audio streams are still loading | media=%s | probe_state=%s",
                media_path,
                probe_state.name.lower(),
            )
            return SubtitleGenerationValidationResult(
                is_valid=False,
                reason=SubtitleGenerationValidationFailure.AUDIO_STREAMS_STILL_LOADING,
            )

        if probe_state == AudioStreamProbeState.FAILED:
            reason = (probe_error or "").strip() or "Audio stream inspection failed."
            if options.audio_stream_index is None:
                logger.warning(
                    "Subtitle generation continuing with default audio because audio stream inspection failed | media=%s",
                    media_path,
                )
                return SubtitleGenerationValidationResult(is_valid=True)

            logger.warning(
                "Subtitle generation aborted because cached audio stream inspection failed during validation | media=%s",
                media_path,
            )
            return SubtitleGenerationValidationResult(
                is_valid=False,
                reason=SubtitleGenerationValidationFailure.AUDIO_STREAM_INSPECTION_FAILED,
                probe_error=reason,
                formatted_reason=self.format_audio_stream_probe_error(reason),
            )

        audio_streams = list(audio_streams or [])

        if options.audio_stream_index is None:
            return SubtitleGenerationValidationResult(is_valid=True)

        if not audio_streams:
            logger.warning("Subtitle generation aborted because media has no audio streams | media=%s", media_path)
            return SubtitleGenerationValidationResult(
                is_valid=False,
                reason=SubtitleGenerationValidationFailure.NO_AUDIO_STREAMS_FOUND,
            )

        available_stream_indices = {stream.stream_index for stream in audio_streams}
        if options.audio_stream_index in available_stream_indices:
            return SubtitleGenerationValidationResult(is_valid=True)

        logger.warning(
            "Subtitle generation aborted because selected audio stream is not available | media=%s | audio_stream_index=%s",
            media_path,
            options.audio_stream_index,
        )
        return SubtitleGenerationValidationResult(
            is_valid=False,
            reason=SubtitleGenerationValidationFailure.AUDIO_STREAM_NO_LONGER_AVAILABLE,
        )

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
            return self._probe_output_parent_write_access(parent_dir, creating_parent=False)

        if parent_dir.anchor and not Path(parent_dir.anchor).exists():
            return "Failed to resolve the destination drive or root folder."

        existing_parent = next((candidate for candidate in (parent_dir, *parent_dir.parents) if candidate.exists()), None)
        if existing_parent is None:
            return "Failed to resolve the destination folder."
        if not existing_parent.is_dir():
            return "The destination folder path points to a file."
        return self._probe_output_parent_write_access(existing_parent, creating_parent=True)

    def _probe_output_parent_write_access(self, probe_dir: Path, *, creating_parent: bool) -> str | None:
        try:
            probe = tempfile.NamedTemporaryFile(
                dir=probe_dir,
                prefix=".a1lplayer-write-probe-",
                suffix=".tmp",
                delete=True,
            )
        except OSError as exc:
            action = "create the destination folder" if creating_parent else "write to the destination folder"
            return f"Failed to {action}: {exc}"

        try:
            probe.write(b"")
            probe.flush()
        except OSError as exc:
            action = "create the destination folder" if creating_parent else "write to the destination folder"
            return f"Failed to {action}: {exc}"
        finally:
            probe.close()

        return None
