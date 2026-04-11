from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
import tempfile

from PySide6.QtWidgets import QWidget

from services.AppTempService import AppTempService
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
        self._last_probed_media_path: str | None = None
        self._last_probed_audio_streams = None
        self._last_probed_audio_stream_error: str | None = None
        self._audio_stream_probe_state = AudioStreamProbeState.IDLE

    def build_generation_audio_tracks(self, media_path: str | None) -> list[tuple[int | None, str]]:
        generated_tracks = self.build_audio_track_choices([])
        if not media_path:
            self.invalidate_audio_stream_probe_cache()
            return generated_tracks

        status = self.get_audio_stream_probe_state(media_path)
        if status == AudioStreamProbeState.FAILED:
            reason = self.get_cached_audio_stream_error_for_media(media_path)
            if reason:
                show_audio_stream_inspection_warning(
                    self._parent,
                    self.format_audio_stream_probe_error(reason),
                )
            return generated_tracks
        if status != AudioStreamProbeState.READY:
            return generated_tracks

        audio_streams = self.get_cached_audio_streams_for_media(media_path) or []

        generated_tracks.extend((stream.stream_index, stream.label) for stream in audio_streams)
        return generated_tracks

    def build_audio_track_choices(self, audio_streams) -> list[tuple[int | None, str]]:
        generated_tracks: list[tuple[int | None, str]] = list(self.DEFAULT_GENERATION_AUDIO_TRACKS)
        generated_tracks.extend((stream.stream_index, stream.label) for stream in audio_streams)
        return generated_tracks

    def get_cached_audio_streams_for_media(self, media_path: str | None):
        normalized_media_path = str(media_path or "")
        if (
            not normalized_media_path
            or self._last_probed_media_path != normalized_media_path
            or self._audio_stream_probe_state != AudioStreamProbeState.READY
        ):
            return None
        return self._last_probed_audio_streams

    def get_cached_audio_stream_error_for_media(self, media_path: str | None) -> str | None:
        normalized_media_path = str(media_path or "")
        if (
            not normalized_media_path
            or self._last_probed_media_path != normalized_media_path
            or self._audio_stream_probe_state != AudioStreamProbeState.FAILED
        ):
            return None
        return self._last_probed_audio_stream_error

    def get_audio_stream_probe_state(self, media_path: str | None) -> AudioStreamProbeState:
        normalized_media_path = str(media_path or "")
        if not normalized_media_path or self._last_probed_media_path != normalized_media_path:
            return AudioStreamProbeState.IDLE
        return self._audio_stream_probe_state

    def begin_audio_stream_probe(self, media_path: str):
        normalized_media_path = str(media_path)
        self._last_probed_media_path = normalized_media_path
        self._last_probed_audio_streams = None
        self._last_probed_audio_stream_error = None
        self._audio_stream_probe_state = AudioStreamProbeState.LOADING

    def abandon_loading_audio_stream_probe(self, media_path: str | None = None):
        normalized_media_path = str(media_path or "")
        if self._audio_stream_probe_state != AudioStreamProbeState.LOADING:
            return
        if normalized_media_path and self._last_probed_media_path != normalized_media_path:
            return
        self.invalidate_audio_stream_probe_cache()

    def cache_audio_stream_probe_success(self, media_path: str, audio_streams):
        normalized_media_path = str(media_path)
        self._last_probed_media_path = normalized_media_path
        self._last_probed_audio_streams = list(audio_streams)
        self._last_probed_audio_stream_error = None
        self._audio_stream_probe_state = AudioStreamProbeState.READY

    def cache_audio_stream_probe_failure(self, media_path: str, reason: str):
        normalized_media_path = str(media_path)
        self._last_probed_media_path = normalized_media_path
        self._last_probed_audio_streams = None
        self._last_probed_audio_stream_error = str(reason).strip() or "Audio stream inspection failed."
        self._audio_stream_probe_state = AudioStreamProbeState.FAILED

    def validate_generation_request(
        self,
        media_path: str,
        options: SubtitleGenerationDialogResult,
    ) -> SubtitleGenerationValidationResult:
        if not self._validate_output_path(options):
            return SubtitleGenerationValidationResult(is_valid=False)

        if not self._validate_audio_stream_selection(media_path, options):
            return SubtitleGenerationValidationResult(is_valid=False)

        return SubtitleGenerationValidationResult(is_valid=True)

    def invalidate_audio_stream_probe_cache(self, media_path: str | None = None):
        if (
            media_path is not None
            and self._last_probed_media_path == media_path
            and self._last_probed_audio_streams is None
            and self._last_probed_audio_stream_error is None
            and self._audio_stream_probe_state == AudioStreamProbeState.IDLE
        ):
            return

        if (
            self._last_probed_media_path is not None
            or self._last_probed_audio_streams is not None
            or self._last_probed_audio_stream_error is not None
        ):
            logger.debug(
                "Invalidating subtitle generation audio stream probe cache | previous_media=%s | next_media=%s | had_error=%s",
                self._last_probed_media_path or "<none>",
                media_path or "<none>",
                bool(self._last_probed_audio_stream_error),
            )

        self._last_probed_media_path = media_path
        self._last_probed_audio_streams = None
        self._last_probed_audio_stream_error = None
        self._audio_stream_probe_state = AudioStreamProbeState.IDLE

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
    ) -> bool:
        probe_state = self.get_audio_stream_probe_state(media_path)
        if probe_state in (AudioStreamProbeState.IDLE, AudioStreamProbeState.LOADING):
            logger.info(
                "Subtitle generation blocked because audio streams are still loading | media=%s | probe_state=%s",
                media_path,
                probe_state.name.lower(),
            )
            show_audio_streams_still_loading(self._parent)
            return False

        if probe_state == AudioStreamProbeState.FAILED:
            reason = self.get_cached_audio_stream_error_for_media(media_path) or "Audio stream inspection failed."
            logger.warning(
                "Subtitle generation aborted because cached audio stream inspection failed during validation | media=%s",
                media_path,
            )
            show_audio_stream_inspection_failed(
                self._parent,
                self.format_audio_stream_probe_error(reason),
            )
            return False

        audio_streams = self.get_cached_audio_streams_for_media(media_path) or []

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

        try:
            parent_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return f"Failed to create the destination folder: {exc}"

        try:
            probe_handle = tempfile.NamedTemporaryFile(
                dir=parent_dir,
                prefix=".subtitle-write-test-",
                suffix=".tmp",
                delete=False,
            )
            probe_path = Path(probe_handle.name)
            probe_handle.close()
            AppTempService.remove_file_if_exists(probe_path, log_context="subtitle output preflight cleanup")
        except OSError as exc:
            return f"Failed to write to the destination folder: {exc}"

        return None
