from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import site
import subprocess
import tempfile
import threading
from services.AppTempService import AppTempService


logger = logging.getLogger(__name__)


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


WINDOWS_CUDA_RUNTIME_PACKAGE_FILES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("nvidia-cublas-cu12", ("nvidia/cublas/bin/cublas64_12.dll",)),
    ("nvidia-cudnn-cu12", ("nvidia/cudnn/bin/cudnn64_9.dll",)),
    ("nvidia-cuda-nvrtc-cu12", ("nvidia/cuda_nvrtc/bin/nvrtc64_120_0.dll",)),
)


def _get_site_package_roots() -> list[Path]:
    candidate_roots: list[Path] = []
    try:
        candidate_roots.append(Path(site.getusersitepackages()))
    except (AttributeError, OSError, TypeError):
        logger.debug("Unable to resolve user site-packages path", exc_info=True)

    try:
        candidate_roots.extend(Path(path) for path in site.getsitepackages())
    except (AttributeError, OSError, TypeError):
        logger.debug("Unable to resolve global site-packages paths", exc_info=True)

    return candidate_roots


def get_missing_windows_cuda_runtime_packages() -> list[str]:
    if os.name != "nt":
        return []

    candidate_roots = _get_site_package_roots()
    missing_packages: list[str] = []

    for package_name, relative_paths in WINDOWS_CUDA_RUNTIME_PACKAGE_FILES:
        package_found = any((root / relative_path).is_file() for root in candidate_roots for relative_path in relative_paths)
        if not package_found:
            missing_packages.append(package_name)

    return missing_packages


def _configure_windows_nvidia_runtime_paths():
    if os.name != "nt":
        return

    candidate_roots = _get_site_package_roots()

    dll_dirs: list[Path] = []
    for root in candidate_roots:
        for relative in {str(Path(relative_path).parent).replace("\\", "/") for _, relative_paths in WINDOWS_CUDA_RUNTIME_PACKAGE_FILES for relative_path in relative_paths}:
            dll_dir = root / relative
            if dll_dir.is_dir():
                dll_dirs.append(dll_dir)

    if not dll_dirs:
        return

    current_path_parts = os.environ.get("PATH", "").split(os.pathsep)
    normalized_existing = {str(Path(path).resolve()) for path in current_path_parts if path}

    for dll_dir in dll_dirs:
        resolved = str(dll_dir.resolve())
        if resolved not in normalized_existing:
            os.environ["PATH"] = resolved + os.pathsep + os.environ.get("PATH", "")
            normalized_existing.add(resolved)
        try:
            os.add_dll_directory(resolved)
        except (AttributeError, FileNotFoundError, OSError):
            logger.debug("Unable to register CUDA DLL directory | path=%s", resolved, exc_info=True)


def _normalize_stream_tag(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _build_audio_stream_label(stream: dict, position: int) -> str:
    tags = stream.get("tags") or {}
    disposition = stream.get("disposition") or {}

    title = _normalize_stream_tag(tags.get("title"))
    language = _normalize_stream_tag(tags.get("language")).lower()
    codec_name = _normalize_stream_tag(stream.get("codec_name")).upper()
    channel_layout = _normalize_stream_tag(stream.get("channel_layout"))
    channels = stream.get("channels")

    parts: list[str] = [f"Audio {position}"]
    if title:
        parts.append(title)
    if language:
        parts.append(language.upper())
    if channel_layout:
        parts.append(channel_layout)
    elif channels:
        parts.append(f"{channels} ch")
    if codec_name:
        parts.append(codec_name)
    if int(disposition.get("default", 0) or 0) == 1:
        parts.append("default")

    return " | ".join(parts)


def probe_audio_streams(media_path: str) -> list[AudioStreamInfo]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=index,codec_name,channels,channel_layout:stream_tags=language,title:stream_disposition=default",
        "-of",
        "json",
        str(media_path),
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ffprobe was not found. Please install ffmpeg/ffprobe to inspect audio streams.") from exc

    if result.returncode != 0:
        error_text = (result.stderr or result.stdout or "Unknown ffprobe error.").strip()
        raise RuntimeError(f"Failed to inspect audio streams: {error_text}")

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("ffprobe returned invalid audio stream metadata.") from exc

    streams = payload.get("streams") or []
    audio_streams: list[AudioStreamInfo] = []
    for position, stream in enumerate(streams, start=1):
        stream_index = stream.get("index")
        if stream_index is None:
            continue
        audio_streams.append(
            AudioStreamInfo(
                stream_index=int(stream_index),
                label=_build_audio_stream_label(stream, position),
                is_default=int((stream.get("disposition") or {}).get("default", 0) or 0) == 1,
            )
        )

    return audio_streams


class SubtitleMaker:
    _PROGRESS_PREPARING = 0
    _PROGRESS_LOADING_MODEL = 10
    _PROGRESS_EXTRACTING_AUDIO = 25
    _PROGRESS_TRANSCRIBING_START = 35
    _PROGRESS_TRANSCRIBING_END = 94
    _PROGRESS_TRANSCRIBING_DONE = 95

    def __init__(self, model_size: str = "small", device: str | None = None):
        self.model_size = model_size
        self.device = device or self._detect_device()
        self._model = None
        self._ffmpeg_process: subprocess.Popen[bytes] | None = None

    def load_model(self):
        if self._model is not None:
            return self._model

        _configure_windows_nvidia_runtime_paths()

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
        self._model = WhisperModel(self.model_size, device=self.device, compute_type=compute_type)
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
            self._raise_if_canceled(cancel_event, "before-model-load")

            if progress_callback is not None:
                progress_callback(
                    "Loading model...",
                    self._PROGRESS_LOADING_MODEL,
                    self._build_stage_details(
                        stage="Loading model",
                        device=self.device,
                        model=self.model_size,
                    ),
                )
            model = self.load_model()
            self._raise_if_canceled(cancel_event, "after-model-load")

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
            return results
        finally:
            if extracted_audio_path is not None:
                self._remove_file_if_exists(extracted_audio_path)

    def save_srt(
        self,
        segments: list[SubtitleSegment],
        output_path: str,
        cancel_event: threading.Event | None = None,
    ):
        def write_srt(handle):
            for index, segment in enumerate(segments, start=1):
                handle.write(f"{index}\n")
                handle.write(
                    f"{self._format_timestamp(segment.start, decimal_separator=',')} --> "
                    f"{self._format_timestamp(segment.end, decimal_separator=',')}\n"
                )
                handle.write(f"{segment.text}\n\n")

        self._write_subtitle_file_atomic(output_path, write_srt, cancel_event=cancel_event)

    def save_vtt(
        self,
        segments: list[SubtitleSegment],
        output_path: str,
        cancel_event: threading.Event | None = None,
    ):
        def write_vtt(handle):
            handle.write("WEBVTT\n\n")
            for segment in segments:
                handle.write(
                    f"{self._format_timestamp(segment.start, decimal_separator='.')} --> "
                    f"{self._format_timestamp(segment.end, decimal_separator='.')}\n"
                )
                handle.write(f"{segment.text}\n\n")

        self._write_subtitle_file_atomic(output_path, write_vtt, cancel_event=cancel_event)

    def save_subtitles(
        self,
        segments: list[SubtitleSegment],
        output_path: str,
        output_format: str,
        cancel_event: threading.Event | None = None,
    ):
        normalized_format = str(output_format).strip().lower()
        logger.info(
            "Saving subtitles | output=%s | format=%s | segments=%s",
            output_path,
            normalized_format or "srt",
            len(segments),
        )
        self._raise_if_canceled(cancel_event, "before-save")
        if normalized_format == "vtt":
            self.save_vtt(segments, output_path, cancel_event=cancel_event)
            return
        self.save_srt(segments, output_path, cancel_event=cancel_event)

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

        try:
            self._ffmpeg_process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
            )
            while True:
                try:
                    stdout_data, stderr_data = self._ffmpeg_process.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    self._raise_if_canceled(cancel_event, "during-audio-extraction")
                    continue
        except FileNotFoundError as exc:
            self._remove_file_if_exists(output_path)
            raise RuntimeError("ffmpeg was not found. Please install ffmpeg to extract a specific audio stream.") from exc
        except SubtitleGenerationCanceledError:
            self.cancel()
            self._remove_file_if_exists(output_path)
            raise
        except (OSError, ValueError, subprocess.SubprocessError):
            self.cancel()
            self._remove_file_if_exists(output_path)
            raise
        finally:
            process = self._ffmpeg_process
            self._ffmpeg_process = None

        if process is None:
            self._remove_file_if_exists(output_path)
            raise RuntimeError("ffmpeg process was not started.")

        if process.returncode is None:
            self._remove_file_if_exists(output_path)
            raise RuntimeError("ffmpeg did not finish correctly.")

        if process.returncode != 0:
            self._remove_file_if_exists(output_path)
            error_text = self._decode_process_output(stderr_data) or self._decode_process_output(stdout_data) or "Unknown ffmpeg error."
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

        return str(output_path)

    def _format_timestamp(self, seconds: float, decimal_separator: str) -> str:
        total_milliseconds = max(0, int(round(float(seconds) * 1000)))
        hours, remainder = divmod(total_milliseconds, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        secs, milliseconds = divmod(remainder, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}{decimal_separator}{milliseconds:03d}"

    def _build_stage_details(
        self,
        stage: str,
        device: str,
        model: str,
        language: str | None = None,
        audio_stream_index: int | None = None,
        source: str | None = None,
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
        process = self._ffmpeg_process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                logger.debug("Best-effort ffmpeg terminate failed", exc_info=True)

    def _decode_process_output(self, payload: bytes | None) -> str:
        if not payload:
            return ""
        return payload.decode("utf-8", errors="replace").strip()

    def _remove_file_if_exists(self, path: str | Path):
        AppTempService.remove_file_if_exists(path, log_context="temporary file cleanup")

    def _write_subtitle_file_atomic(
        self,
        output_path: str,
        writer,
        cancel_event: threading.Event | None = None,
    ):
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        temp_handle = None
        temp_path: str | None = None

        try:
            temp_handle = tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=output_file.parent,
                delete=False,
                suffix=output_file.suffix,
            )
            temp_path = temp_handle.name
            self._raise_if_canceled(cancel_event, "before-write-temp-subtitle")
            writer(temp_handle)
            temp_handle.flush()
            temp_handle.close()
            temp_handle = None
            self._raise_if_canceled(cancel_event, "before-atomic-replace")
            os.replace(temp_path, output_file)
        except SubtitleGenerationCanceledError:
            if temp_handle is not None:
                temp_handle.close()
            if temp_path is not None:
                self._remove_file_if_exists(temp_path)
            raise
        except (OSError, ValueError):
            logger.exception("Atomic subtitle save failed | output=%s", output_path)
            if temp_handle is not None:
                temp_handle.close()
            if temp_path is not None:
                self._remove_file_if_exists(temp_path)
            raise
