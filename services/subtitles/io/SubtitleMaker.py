import logging
import os
from pathlib import Path
import subprocess
import threading
import time
from services.app.AppTempService import AppTempService
from services.runtime.SubprocessLifecycle import SubprocessLifecycleMixin
from services.subtitles.io.AudioStreamProbe import FFPROBE_AUDIO_STREAM_TIMEOUT_SECONDS, probe_audio_streams
from services.subtitles.domain.CudaRuntimeDiscovery import (
    WINDOWS_CUDA_RUNTIME_PACKAGE_FILES,
    configure_windows_nvidia_runtime_paths,
    get_missing_windows_cuda_runtime_packages,
)
from services.subtitles.io.SubtitleFileWriter import SubtitleFileWriter
from services.subtitles.domain.SubtitleTiming import elapsed_ms_since, log_timing
from services.subtitles.domain.SubtitleTypes import (
    AudioStreamInfo,
    SubtitleGenerationCanceledError,
    SubtitleGenerationEmptyResultError,
    SubtitleSegment,
)
from services.runtime.SubprocessWorkerSupport import BoundedLineBuffer


logger = logging.getLogger(__name__)

_configure_windows_nvidia_runtime_paths = configure_windows_nvidia_runtime_paths


class SubtitleMaker(SubprocessLifecycleMixin):
    _PROGRESS_PREPARING = 0
    _PROGRESS_LOADING_MODEL = 10
    _PROGRESS_MODEL_READY = 20
    _PROGRESS_EXTRACTING_AUDIO = 25
    _PROGRESS_TRANSCRIBING_START = 35
    _PROGRESS_TRANSCRIBING_END = 94
    _PROGRESS_TRANSCRIBING_DONE = 95

    def __init__(self, model_size: str = "small", device: str | None = None):
        self.model_size = model_size
        self.device = device or self._detect_device()
        self._model = None
        self._init_subprocess_lifecycle()

    def load_model(self):
        if self._model is not None:
            return self._model

        configure_windows_nvidia_runtime_paths()

        try:
            from faster_whisper import WhisperModel
        except ModuleNotFoundError as exc:
            raise RuntimeError("faster-whisper is not installed.") from exc

        if self.device == "cuda" and self._detect_device() != "cuda":
            raise RuntimeError("CUDA was selected, but no compatible CUDA device is available.")

        compute_type = "int8" if self.device == "cpu" else "float16"
        logger.info(
            "Loading whisper model | model=%s | device=%s | compute_type=%s",
            self.model_size,
            self.device,
            compute_type,
        )
        model_init_started_at = time.perf_counter()
        self._model = WhisperModel(self.model_size, device=self.device, compute_type=compute_type)
        log_timing(
            logger,
            "Subtitle helper timing",
            "model_init",
            elapsed_ms_since(model_init_started_at),
            model=self.model_size,
            requested_device=self.device,
            actual_device=self.device,
            compute_type=compute_type,
        )
        return self._model

    def _raise_if_canceled(self, cancel_event: threading.Event | None, context: str | None = None):
        if cancel_event is None or not cancel_event.is_set():
            return
        if context:
            logger.info("Subtitle generation canceled cooperatively | context=%s", context)
        raise SubtitleGenerationCanceledError()

    def transcribe_file(
        self,
        media_path: str,
        audio_stream_index: int | None = None,
        language: str | None = None,
        progress_callback=None,
        cancel_event: threading.Event | None = None,
    ) -> list[SubtitleSegment]:
        source_path = str(media_path)
        extracted_audio_path: str | None = None
        logger.info(
            "Subtitle transcription started | media=%s | audio_stream_index=%s | language=%s | device=%s | model=%s",
            media_path,
            audio_stream_index,
            language or "auto",
            self.device,
            self.model_size,
        )

        if progress_callback is not None:
            progress_callback(
                "Preparing...",
                self._PROGRESS_PREPARING,
                self._build_stage_details(
                    stage="Preparing",
                    device=self.device,
                    model=self.model_size,
                    source=source_path,
                ),
            )

        try:
            transcribe_call_started_at = time.perf_counter()
            self._raise_if_canceled(cancel_event, "before-model-load")

            if progress_callback is not None:
                progress_callback(
                    "Loading speech model...",
                    self._PROGRESS_LOADING_MODEL,
                    self._build_stage_details(
                        stage="Loading speech model",
                        device=self.device,
                        model=self.model_size,
                        note="First run may download model files. This can take a while depending on disk and network speed.",
                    ),
                )
            model = self.load_model()
            self._raise_if_canceled(cancel_event, "after-model-load")
            if progress_callback is not None:
                progress_callback(
                    "Speech model ready.",
                    self._PROGRESS_MODEL_READY,
                    self._build_stage_details(
                        stage="Speech model ready",
                        device=self.device,
                        model=self.model_size,
                    ),
                )

            if audio_stream_index is not None:
                self._raise_if_canceled(cancel_event, "before-audio-extraction")
                if progress_callback is not None:
                    progress_callback(
                        "Extracting audio...",
                        self._PROGRESS_EXTRACTING_AUDIO,
                        self._build_stage_details(
                            stage="Extracting audio",
                            device=self.device,
                            model=self.model_size,
                            audio_stream_index=audio_stream_index,
                        ),
                    )
                extracted_audio_path = self._extract_audio_stream(
                    source_path,
                    int(audio_stream_index),
                    cancel_event=cancel_event,
                )
                source_path = extracted_audio_path
                self._raise_if_canceled(cancel_event, "after-audio-extraction")

            self._raise_if_canceled(cancel_event, "before-transcription")

            if progress_callback is not None:
                progress_callback(
                    "Transcribing audio...",
                    self._PROGRESS_TRANSCRIBING_START,
                    self._build_stage_details(
                        stage="Transcribing",
                        device=self.device,
                        model=self.model_size,
                        language=language or "auto",
                    ),
                )

            transcription_started_at = time.perf_counter()
            segments_iter, info = model.transcribe(
                source_path,
                language=language,
                task="transcribe",
                vad_filter=True,
            )
            self._raise_if_canceled(cancel_event, "after-transcription-start")

            total_duration = float(getattr(info, "duration", 0.0) or 0.0)
            detected_language = getattr(info, "language", None) or (language or "auto")
            progress_details = self._build_stage_details(
                stage="Transcribing",
                device=self.device,
                model=self.model_size,
                language=detected_language,
            )
            results: list[SubtitleSegment] = []

            for segment in segments_iter:
                if cancel_event is not None and cancel_event.is_set():
                    raise SubtitleGenerationCanceledError()

                text = str(segment.text).strip()
                if text:
                    results.append(
                        SubtitleSegment(
                            start=float(segment.start),
                            end=float(segment.end),
                            text=text,
                        )
                    )

                if progress_callback is not None and total_duration > 0:
                    transcribe_ratio = max(0.0, min(1.0, float(segment.end) / total_duration))
                    progress = self._PROGRESS_TRANSCRIBING_START + int(
                        transcribe_ratio * (self._PROGRESS_TRANSCRIBING_END - self._PROGRESS_TRANSCRIBING_START)
                    )
                    progress_callback("Transcribing audio...", progress, progress_details)

            self._raise_if_canceled(cancel_event, "after-transcription-loop")
            if progress_callback is not None:
                progress_callback("Transcription finished.", self._PROGRESS_TRANSCRIBING_DONE, progress_details)

            if not results:
                raise SubtitleGenerationEmptyResultError("No speech was detected. Subtitle file was not created.")

            logger.info(
                "Subtitle transcription completed | media=%s | segments=%s | detected_language=%s",
                media_path,
                len(results),
                detected_language,
            )
            log_timing(
                logger,
                "Subtitle helper timing",
                "transcription",
                elapsed_ms_since(transcription_started_at),
                media=media_path,
                segments=len(results),
                detected_language=detected_language,
                actual_device=self.device,
                model_size=self.model_size,
            )
            return results
        finally:
            log_timing(
                logger,
                "Subtitle helper timing",
                "transcribe_call_total",
                elapsed_ms_since(transcribe_call_started_at),
                media=media_path,
                audio_stream_index=audio_stream_index,
                requested_language=language or "auto",
                actual_device=self.device,
                model_size=self.model_size,
            )
            if extracted_audio_path is not None:
                self._remove_file_if_exists(extracted_audio_path)

    def save_srt(
        self,
        segments: list[SubtitleSegment],
        output_path: str,
        cancel_event: threading.Event | None = None,
        overwrite_confirmed_for_path: str | None = None,
        allow_unconfirmed_overwrite: bool = True,
    ) -> str:
        return SubtitleFileWriter(self._raise_if_canceled).save_srt(
            segments,
            output_path,
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
        return SubtitleFileWriter(self._raise_if_canceled).save_vtt(
            segments,
            output_path,
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
        return SubtitleFileWriter(self._raise_if_canceled).save_subtitles(
            segments,
            output_path,
            output_format,
            cancel_event=cancel_event,
            overwrite_confirmed_for_path=overwrite_confirmed_for_path,
            allow_unconfirmed_overwrite=allow_unconfirmed_overwrite,
        )

    def _extract_audio_stream(
        self,
        media_path: str,
        audio_stream_index: int,
        cancel_event: threading.Event | None = None,
    ) -> str:
        logger.info(
            "Extracting audio stream for subtitle generation | media=%s | audio_stream_index=%s",
            media_path,
            audio_stream_index,
        )
        output_path = AppTempService.create_subtitle_generation_file_path(
            suffix=".wav",
            prefix="extracted-audio-",
        )

        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(media_path),
            "-map",
            f"0:{audio_stream_index}",
            "-vn",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "16000",
            "-ac",
            "1",
            str(output_path),
        ]

        audio_extract_started_at = time.perf_counter()
        process: subprocess.Popen[str] | None = None
        stderr_buffer = BoundedLineBuffer(max_lines=200)
        stderr_thread: threading.Thread | None = None
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                **self._subprocess_spawn_options(),
            )
            self._set_active_process(process)
            if process.stderr is not None:
                stderr_thread = threading.Thread(
                    target=self._collect_process_stream,
                    args=(process.stderr, stderr_buffer, "stderr"),
                    name="ffmpeg audio extraction stderr reader",
                    daemon=True,
                )
                stderr_thread.start()

            while True:
                try:
                    process.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    self._raise_if_canceled(cancel_event, "during-audio-extraction")
                    continue
        except FileNotFoundError as exc:
            self._remove_file_if_exists(output_path)
            raise RuntimeError("ffmpeg was not found. Please install ffmpeg to extract a specific audio stream.") from exc
        except SubtitleGenerationCanceledError:
            self.cancel()
            self._wait_for_process_stop(process)
            self._remove_file_if_exists(output_path)
            raise
        except (OSError, ValueError, subprocess.SubprocessError):
            self.cancel()
            self._wait_for_process_stop(process)
            self._remove_file_if_exists(output_path)
            raise
        finally:
            self._clear_active_process(process)
            if process is not None:
                self._close_stream(process.stderr)
            if stderr_thread is not None:
                self._join_process_reader(stderr_thread, timeout=0.5, stream_name="stderr")

        if process is None:
            self._remove_file_if_exists(output_path)
            raise RuntimeError("ffmpeg process was not started.")

        if process.returncode is None:
            self._remove_file_if_exists(output_path)
            raise RuntimeError("ffmpeg did not finish correctly.")

        if process.returncode != 0:
            self._remove_file_if_exists(output_path)
            error_text = stderr_buffer.consume_text() or "Unknown ffmpeg error."
            if process.returncode < 0:
                raise SubtitleGenerationCanceledError()
            logger.error(
                "ffmpeg audio extraction failed | media=%s | audio_stream_index=%s | returncode=%s | details=%s",
                media_path,
                audio_stream_index,
                process.returncode,
                error_text,
            )
            raise RuntimeError(f"Failed to extract audio stream: {error_text}")

        log_timing(
            logger,
            "Subtitle helper timing",
            "audio_extract",
            elapsed_ms_since(audio_extract_started_at),
            media=media_path,
            audio_stream_index=audio_stream_index,
            output=output_path,
        )
        return str(output_path)

    def _build_stage_details(
        self,
        stage: str,
        device: str,
        model: str,
        language: str | None = None,
        audio_stream_index: int | None = None,
        source: str | None = None,
        note: str | None = None,
    ) -> str:
        lines = [
            f"Stage: {stage}",
            f"Device: {device}",
            f"Model: {model}",
        ]
        if language is not None:
            lines.append(f"Language: {language}")
        if audio_stream_index is not None:
            lines.append(f"Audio stream: #{int(audio_stream_index)}")
        if source is not None:
            lines.append(f"Source: {source}")
        if note is not None:
            lines.append(str(note))
        return "\n".join(lines)

    def _detect_device(self) -> str:
        try:
            import ctranslate2
            if int(ctranslate2.get_cuda_device_count()) > 0:
                return "cuda"
        except (ImportError, AttributeError, TypeError, ValueError, OSError):
            logger.debug("CUDA detection failed; falling back to CPU", exc_info=True)
        return "cpu"

    def cancel(self):
        self._begin_termination()

    def _wait_for_process_stop(self, process: subprocess.Popen[str] | None):
        if process is None or process.poll() is not None:
            return
        try:
            process.wait(timeout=self._graceful_cancel_timeout_seconds() + 0.5)
        except subprocess.TimeoutExpired:
            logger.warning("ffmpeg audio extraction process did not stop after cancellation | pid=%s", process.pid)

    def _collect_process_stream(self, stream, buffer: BoundedLineBuffer, stream_name: str):
        try:
            for line in stream:
                text = str(line).rstrip()
                if text:
                    buffer.append(text)
        except OSError:
            logger.debug("ffmpeg %s stream closed during audio extraction", stream_name)

    def _join_process_reader(self, thread: threading.Thread, *, timeout: float, stream_name: str):
        thread.join(timeout=timeout)
        if thread.is_alive():
            logger.warning("ffmpeg audio extraction %s reader did not stop within %.1fs", stream_name, timeout)

    def _close_stream(self, stream):
        if stream is None:
            return
        try:
            stream.close()
        except OSError:
            logger.debug("Best-effort ffmpeg stream close failed", exc_info=True)

    def _remove_file_if_exists(self, path: str | Path):
        AppTempService.remove_file_if_exists(path, log_context="temporary file cleanup")
