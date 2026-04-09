from dataclasses import dataclass
import os
from pathlib import Path
import site
import subprocess
import tempfile
import threading


class SubtitleGenerationCanceledError(RuntimeError):
    pass


class SubtitleGenerationEmptyResultError(RuntimeError):
    pass


@dataclass
class SubtitleSegment:
    start: float
    end: float
    text: str


WINDOWS_CUDA_RUNTIME_PACKAGE_FILES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("nvidia-cublas-cu12", ("nvidia/cublas/bin/cublas64_12.dll",)),
    ("nvidia-cudnn-cu12", ("nvidia/cudnn/bin/cudnn64_9.dll",)),
    ("nvidia-cuda-nvrtc-cu12", ("nvidia/cuda_nvrtc/bin/nvrtc64_120_0.dll",)),
)


def _get_site_package_roots() -> list[Path]:
    candidate_roots: list[Path] = []
    try:
        candidate_roots.append(Path(site.getusersitepackages()))
    except Exception:
        pass

    try:
        candidate_roots.extend(Path(path) for path in site.getsitepackages())
    except Exception:
        pass

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
            pass


class SubtitleMaker:
    _MODEL_CACHE: dict[tuple[str, str], object] = {}
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

        cache_key = (self.model_size, self.device)
        cached_model = self._MODEL_CACHE.get(cache_key)
        if cached_model is not None:
            self._model = cached_model
            return self._model

        compute_type = "int8" if self.device == "cpu" else "float16"
        self._model = WhisperModel(self.model_size, device=self.device, compute_type=compute_type)
        self._MODEL_CACHE[cache_key] = self._model
        return self._model

    def transcribe_file(
        self,
        media_path: str,
        audio_track: int | None = None,
        language: str | None = None,
        progress_callback=None,
        cancel_event: threading.Event | None = None,
    ) -> list[SubtitleSegment]:
        source_path = str(media_path)
        extracted_audio_path: str | None = None

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
            if cancel_event is not None and cancel_event.is_set():
                raise SubtitleGenerationCanceledError()

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

            if audio_track is not None:
                if cancel_event is not None and cancel_event.is_set():
                    raise SubtitleGenerationCanceledError()
                if progress_callback is not None:
                    progress_callback(
                        "Extracting audio...",
                        self._PROGRESS_EXTRACTING_AUDIO,
                        self._build_stage_details(
                            stage="Extracting audio",
                            device=self.device,
                            model=self.model_size,
                            audio_track=audio_track,
                        ),
                    )
                extracted_audio_path = self._extract_audio_track(source_path, int(audio_track))
                source_path = extracted_audio_path

            if cancel_event is not None and cancel_event.is_set():
                raise SubtitleGenerationCanceledError()

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

            if progress_callback is not None:
                progress_callback("Transcription finished.", self._PROGRESS_TRANSCRIBING_DONE, progress_details)

            if not results:
                raise SubtitleGenerationEmptyResultError("No speech was detected. Subtitle file was not created.")

            return results
        finally:
            if extracted_audio_path is not None:
                self._remove_file_if_exists(extracted_audio_path)

    def save_srt(self, segments: list[SubtitleSegment], output_path: str):
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        with output_file.open("w", encoding="utf-8") as handle:
            for index, segment in enumerate(segments, start=1):
                handle.write(f"{index}\n")
                handle.write(
                    f"{self._format_timestamp(segment.start, decimal_separator=',')} --> "
                    f"{self._format_timestamp(segment.end, decimal_separator=',')}\n"
                )
                handle.write(f"{segment.text}\n\n")

    def save_vtt(self, segments: list[SubtitleSegment], output_path: str):
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        with output_file.open("w", encoding="utf-8") as handle:
            handle.write("WEBVTT\n\n")
            for segment in segments:
                handle.write(
                    f"{self._format_timestamp(segment.start, decimal_separator='.')} --> "
                    f"{self._format_timestamp(segment.end, decimal_separator='.')}\n"
                )
                handle.write(f"{segment.text}\n\n")

    def save_subtitles(self, segments: list[SubtitleSegment], output_path: str, output_format: str):
        normalized_format = str(output_format).strip().lower()
        if normalized_format == "vtt":
            self.save_vtt(segments, output_path)
            return
        self.save_srt(segments, output_path)

    def _extract_audio_track(self, media_path: str, audio_track: int) -> str:
        output_handle = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        output_path = Path(output_handle.name)
        output_handle.close()

        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(media_path),
            "-map",
            f"0:a:{audio_track}",
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
            stdout_data, stderr_data = self._ffmpeg_process.communicate()
        except FileNotFoundError as exc:
            self._remove_file_if_exists(output_path)
            raise RuntimeError("ffmpeg was not found. Please install ffmpeg to extract a specific audio track.") from exc
        except Exception:
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
            raise RuntimeError(f"Failed to extract audio track: {error_text}")

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
        audio_track: int | None = None,
        source: str | None = None,
    ) -> str:
        lines = [
            f"Stage: {stage}",
            f"Device: {device}",
            f"Model: {model}",
        ]
        if language is not None:
            lines.append(f"Language: {language}")
        if audio_track is not None:
            lines.append(f"Audio track: {int(audio_track) + 1}")
        if source is not None:
            lines.append(f"Source: {source}")
        return "\n".join(lines)

    def _detect_device(self) -> str:
        try:
            import ctranslate2
            if int(ctranslate2.get_cuda_device_count()) > 0:
                return "cuda"
        except Exception:
            pass
        return "cpu"

    @classmethod
    def clear_model_cache(cls):
        cls._MODEL_CACHE.clear()

    def cancel(self):
        process = self._ffmpeg_process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def _decode_process_output(self, payload: bytes | None) -> str:
        if not payload:
            return ""
        return payload.decode("utf-8", errors="replace").strip()

    def _remove_file_if_exists(self, path: str | Path):
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
