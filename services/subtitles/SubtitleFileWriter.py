from collections.abc import Callable
import logging
import os
from pathlib import Path
import tempfile
import threading
import time

from services.AppTempService import AppTempService
from services.subtitles.SubtitleTiming import elapsed_ms_since, log_timing
from services.subtitles.SubtitleTypes import SubtitleGenerationCanceledError, SubtitleSegment


logger = logging.getLogger(__name__)


class SubtitleFileWriter:
    def __init__(self, raise_if_canceled: Callable[[threading.Event | None, str | None], None]):
        self._raise_if_canceled = raise_if_canceled

    def save_srt(
        self,
        segments: list[SubtitleSegment],
        output_path: str,
        cancel_event: threading.Event | None = None,
        overwrite_confirmed_for_path: str | None = None,
        allow_unconfirmed_overwrite: bool = True,
    ) -> str:
        def write_srt(handle):
            for index, segment in enumerate(segments, start=1):
                handle.write(f"{index}\n")
                handle.write(
                    f"{self._format_timestamp(segment.start, decimal_separator=',')} --> "
                    f"{self._format_timestamp(segment.end, decimal_separator=',')}\n"
                )
                handle.write(f"{segment.text}\n\n")

        return self._write_subtitle_file_atomic(
            output_path,
            write_srt,
            cancel_event=cancel_event,
            overwrite_confirmed_for_path=overwrite_confirmed_for_path,
            allow_unconfirmed_overwrite=allow_unconfirmed_overwrite,
        )

    def save_vtt(
        self,
        segments: list[SubtitleSegment],
        output_path: str,
        cancel_event: threading.Event | None = None,
        overwrite_confirmed_for_path: str | None = None,
        allow_unconfirmed_overwrite: bool = True,
    ) -> str:
        def write_vtt(handle):
            handle.write("WEBVTT\n\n")
            for segment in segments:
                handle.write(
                    f"{self._format_timestamp(segment.start, decimal_separator='.')} --> "
                    f"{self._format_timestamp(segment.end, decimal_separator='.')}\n"
                )
                handle.write(f"{segment.text}\n\n")

        return self._write_subtitle_file_atomic(
            output_path,
            write_vtt,
            cancel_event=cancel_event,
            overwrite_confirmed_for_path=overwrite_confirmed_for_path,
            allow_unconfirmed_overwrite=allow_unconfirmed_overwrite,
        )

    def save_subtitles(
        self,
        segments: list[SubtitleSegment],
        output_path: str,
        output_format: str,
        cancel_event: threading.Event | None = None,
        overwrite_confirmed_for_path: str | None = None,
        allow_unconfirmed_overwrite: bool = True,
    ) -> str:
        normalized_format = str(output_format).strip().lower()
        logger.info(
            "Saving subtitles | output=%s | format=%s | segments=%s",
            output_path,
            normalized_format or "srt",
            len(segments),
        )
        self._raise_if_canceled(cancel_event, "before-save")
        save_started_at = time.perf_counter()
        if normalized_format == "vtt":
            saved_output_path = self.save_vtt(
                segments,
                output_path,
                cancel_event=cancel_event,
                overwrite_confirmed_for_path=overwrite_confirmed_for_path,
                allow_unconfirmed_overwrite=allow_unconfirmed_overwrite,
            )
        else:
            saved_output_path = self.save_srt(
                segments,
                output_path,
                cancel_event=cancel_event,
                overwrite_confirmed_for_path=overwrite_confirmed_for_path,
                allow_unconfirmed_overwrite=allow_unconfirmed_overwrite,
            )
        log_timing(
            logger,
            "Subtitle helper timing",
            "subtitle_save",
            elapsed_ms_since(save_started_at),
            output=saved_output_path,
            format=normalized_format or "srt",
            segments=len(segments),
        )
        return saved_output_path

    def _format_timestamp(self, seconds: float, decimal_separator: str) -> str:
        total_milliseconds = max(0, int(round(float(seconds) * 1000)))
        hours, remainder = divmod(total_milliseconds, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        secs, milliseconds = divmod(remainder, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}{decimal_separator}{milliseconds:03d}"

    def _remove_file_if_exists(self, path: str | Path):
        AppTempService.remove_file_if_exists(path, log_context="temporary file cleanup")

    def _prepare_output_path_for_write(self, output_path: str | Path) -> Path:
        output_file = Path(output_path)
        parent_dir = output_file.parent
        if parent_dir.exists() and not parent_dir.is_dir():
            raise RuntimeError("The destination folder path points to a file.")
        try:
            parent_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeError(f"Failed to create the destination folder: {exc}") from exc
        if not parent_dir.is_dir():
            raise RuntimeError("The destination folder path points to a file.")
        return output_file

    def _create_temp_subtitle_file(self, output_file: Path):
        try:
            return tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=output_file.parent,
                delete=False,
                suffix=output_file.suffix,
            )
        except OSError as exc:
            raise RuntimeError(f"Failed to write to the destination folder: {exc}") from exc

    def _write_subtitle_file_atomic(
        self,
        output_path: str,
        writer,
        cancel_event: threading.Event | None = None,
        overwrite_confirmed_for_path: str | None = None,
        allow_unconfirmed_overwrite: bool = True,
    ) -> str:
        output_file = self._prepare_output_path_for_write(output_path)
        self._validate_requested_output_path(
            output_file,
            overwrite_confirmed_for_path=overwrite_confirmed_for_path,
        )
        write_target = self._resolve_output_write_target(
            output_file,
            overwrite_confirmed_for_path=overwrite_confirmed_for_path,
            allow_unconfirmed_overwrite=allow_unconfirmed_overwrite,
        )

        temp_handle = None
        temp_path: str | None = None

        atomic_write_started_at = time.perf_counter()
        try:
            temp_handle = self._create_temp_subtitle_file(output_file)
            temp_path = temp_handle.name
            self._raise_if_canceled(cancel_event, "before-write-temp-subtitle")
            writer(temp_handle)
            temp_handle.flush()
            temp_handle.close()
            temp_handle = None
            self._raise_if_canceled(cancel_event, "before-atomic-replace")
            write_target = self._resolve_output_write_target(
                output_file,
                overwrite_confirmed_for_path=overwrite_confirmed_for_path,
                allow_unconfirmed_overwrite=allow_unconfirmed_overwrite,
            )
            try:
                if write_target == output_file:
                    os.replace(temp_path, write_target)
                    final_output_path = str(write_target)
                else:
                    final_output_path = str(self._write_temp_file_to_fallback(temp_path, output_file))
            except PermissionError:
                logger.warning(
                    "Atomic subtitle overwrite failed because destination is in use; saving with fallback name | requested_output=%s",
                    output_file,
                )
                fallback_output_file = self._write_temp_file_to_fallback(temp_path, output_file)
                final_output_path = str(fallback_output_file)
            log_timing(
                logger,
                "Subtitle helper timing",
                "save_atomic_write",
                elapsed_ms_since(atomic_write_started_at),
                output=final_output_path,
                temp_path=temp_path,
            )
            return final_output_path
        except SubtitleGenerationCanceledError:
            if temp_handle is not None:
                temp_handle.close()
            if temp_path is not None:
                self._remove_file_if_exists(temp_path)
            raise
        except RuntimeError:
            logger.exception("Atomic subtitle save failed | output=%s", output_path)
            if temp_handle is not None:
                temp_handle.close()
            if temp_path is not None:
                self._remove_file_if_exists(temp_path)
            raise
        except (OSError, ValueError) as exc:
            logger.exception("Atomic subtitle save failed | output=%s", output_path)
            if temp_handle is not None:
                temp_handle.close()
            if temp_path is not None:
                self._remove_file_if_exists(temp_path)
            raise RuntimeError(f"Failed to write subtitle file: {exc}") from exc

    def _build_fallback_subtitle_output_path(self, requested_output_path: Path) -> Path:
        for candidate in self._iter_fallback_subtitle_output_paths(requested_output_path):
            if not candidate.exists():
                return candidate

        raise RuntimeError(f"Could not allocate a fallback subtitle output path for {requested_output_path}")

    def _write_temp_file_to_fallback(self, temp_path: str, requested_output_path: Path) -> Path:
        for candidate in self._iter_fallback_subtitle_output_paths(requested_output_path):
            reserved_fd: int | None = None
            try:
                reserved_fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            except FileExistsError:
                continue
            except OSError as exc:
                raise RuntimeError(f"Failed to reserve fallback subtitle output path: {exc}") from exc

            try:
                os.close(reserved_fd)
                reserved_fd = None
                os.replace(temp_path, candidate)
            except OSError as exc:
                if reserved_fd is not None:
                    os.close(reserved_fd)
                self._remove_file_if_exists(candidate)
                raise RuntimeError(f"Failed to write fallback subtitle file: {exc}") from exc

            return candidate

        raise RuntimeError(f"Could not allocate a fallback subtitle output path for {requested_output_path}")

    def _iter_fallback_subtitle_output_paths(self, requested_output_path: Path):
        parent_dir = requested_output_path.parent
        stem = requested_output_path.stem
        suffix = requested_output_path.suffix

        for index in range(1, 1000):
            yield parent_dir / f"{stem} ({index}){suffix}"

    def _resolve_output_write_target(
        self,
        output_file: Path,
        *,
        overwrite_confirmed_for_path: str | None,
        allow_unconfirmed_overwrite: bool,
    ) -> Path:
        if output_file.exists():
            if output_file.is_dir():
                raise RuntimeError("The destination output path points to a folder.")
            if not allow_unconfirmed_overwrite and not self._confirmed_path_matches(
                output_file,
                overwrite_confirmed_for_path,
            ):
                fallback_output_file = self._build_fallback_subtitle_output_path(output_file)
                logger.warning(
                    "Subtitle output appeared after validation; saving with fallback name | requested_output=%s | fallback_output=%s",
                    output_file,
                    fallback_output_file,
                )
                return fallback_output_file
        return output_file

    def _validate_requested_output_path(
        self,
        output_file: Path,
        *,
        overwrite_confirmed_for_path: str | None,
    ):
        if overwrite_confirmed_for_path is None:
            return
        if not self._same_normalized_path(output_file, Path(overwrite_confirmed_for_path)):
            raise RuntimeError("Overwrite confirmation does not match the requested output path.")

    def _confirmed_path_matches(self, output_file: Path, confirmed_path: str | None) -> bool:
        return confirmed_path is not None and self._same_normalized_path(output_file, Path(confirmed_path))

    def _same_normalized_path(self, left: Path, right: Path) -> bool:
        try:
            return os.path.normcase(str(left.expanduser().resolve(strict=False))) == os.path.normcase(
                str(right.expanduser().resolve(strict=False))
            )
        except (OSError, RuntimeError, ValueError):
            return False
