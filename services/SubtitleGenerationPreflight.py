from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
import tempfile

from PySide6.QtWidgets import QWidget

from services.AppTempService import AppTempService
from services.SubtitleMaker import probe_audio_streams
from ui.MessageBoxService import (
    confirm_overwrite_subtitle,
    show_audio_stream_inspection_failed,
    show_audio_stream_inspection_warning,
    show_audio_stream_no_longer_available,
    show_choose_output_path_first,
    show_no_audio_streams_found,
    show_subtitle_output_path_unavailable,
)
from ui.SubtitleGenerationDialog import SubtitleGenerationDialogResult


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubtitleGenerationValidationResult:
    is_valid: bool


class SubtitleGenerationPreflight:
    def __init__(self, parent: QWidget):
        self._parent = parent
        self._last_probed_media_path: str | None = None
        self._last_probed_audio_streams = None
        self._last_probed_audio_stream_error: str | None = None

    def build_generation_audio_tracks(self, media_path: str | None) -> list[tuple[int | None, str]]:
        generated_tracks: list[tuple[int | None, str]] = [(None, "Current / default")]
        if not media_path:
            self.invalidate_audio_stream_probe_cache()
            return generated_tracks

        try:
            audio_streams = self.get_audio_streams_for_media(media_path)
        except Exception as exc:
            logger.exception(
                "Failed to inspect audio streams for subtitle generation | media=%s",
                media_path,
            )
            show_audio_stream_inspection_warning(
                self._parent,
                self.format_audio_stream_probe_error(str(exc)),
            )
            return generated_tracks

        generated_tracks.extend((stream.stream_index, stream.label) for stream in audio_streams)
        return generated_tracks

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

    def get_audio_streams_for_media(self, media_path: str):
        normalized_media_path = str(media_path)
        if self._last_probed_media_path != normalized_media_path:
            self.invalidate_audio_stream_probe_cache(normalized_media_path)

        if self._last_probed_audio_streams is not None:
            logger.debug(
                "Reusing cached audio stream probe for subtitle generation | media=%s | stream_count=%s",
                normalized_media_path,
                len(self._last_probed_audio_streams),
            )
            return self._last_probed_audio_streams

        if self._last_probed_audio_stream_error is not None:
            logger.debug(
                "Reusing cached audio stream probe failure for subtitle generation | media=%s | reason=%s",
                normalized_media_path,
                self._last_probed_audio_stream_error,
            )
            raise RuntimeError(self._last_probed_audio_stream_error)

        logger.debug("Audio stream probe cache miss for subtitle generation | media=%s", normalized_media_path)
        try:
            audio_streams = probe_audio_streams(normalized_media_path)
        except Exception as exc:
            self._last_probed_media_path = normalized_media_path
            self._last_probed_audio_streams = None
            self._last_probed_audio_stream_error = str(exc).strip() or "Audio stream inspection failed."
            raise RuntimeError(self._last_probed_audio_stream_error) from exc

        self._last_probed_media_path = normalized_media_path
        self._last_probed_audio_streams = audio_streams
        self._last_probed_audio_stream_error = None
        return audio_streams

    def invalidate_audio_stream_probe_cache(self, media_path: str | None = None):
        if (
            media_path is not None
            and self._last_probed_media_path == media_path
            and self._last_probed_audio_streams is None
            and self._last_probed_audio_stream_error is None
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

    def format_audio_stream_probe_error(self, reason: str) -> str:
        normalized_reason = (reason or "").strip() or "Audio stream inspection failed."
        if normalized_reason.lower().startswith("ffprobe was not found"):
            return "ffprobe was not found. Please install ffmpeg/ffprobe to inspect audio streams."
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
        try:
            audio_streams = self.get_audio_streams_for_media(media_path)
        except Exception as exc:
            logger.exception(
                "Subtitle generation aborted because audio stream inspection failed during validation | media=%s",
                media_path,
            )
            show_audio_stream_inspection_failed(
                self._parent,
                self.format_audio_stream_probe_error(str(exc)),
            )
            return False

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
