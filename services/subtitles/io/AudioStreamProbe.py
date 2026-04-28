import json
import logging
import subprocess

from services.subtitles.domain.SubtitleTypes import AudioStreamInfo
from utils.runtime_assets import resolve_runtime_executable


logger = logging.getLogger(__name__)

FFPROBE_AUDIO_STREAM_TIMEOUT_SECONDS = 15.0


def build_audio_stream_probe_command(media_path: str) -> list[str]:
    return [
        resolve_runtime_executable("ffprobe"),
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


def parse_audio_stream_probe_output(media_path: str, stdout: str) -> list[AudioStreamInfo]:
    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError as exc:
        logger.error(
            "ffprobe audio stream inspection returned invalid JSON | media=%s | details=%s",
            media_path,
            exc,
        )
        raise RuntimeError("ffprobe returned invalid audio stream metadata.") from exc

    if not isinstance(payload, dict):
        logger.error(
            "ffprobe audio stream inspection returned unexpected payload type | media=%s | payload_type=%s",
            media_path,
            type(payload).__name__,
        )
        raise RuntimeError("ffprobe returned an unexpected audio stream response.")

    streams = payload.get("streams") or []
    if not isinstance(streams, list):
        logger.error(
            "ffprobe audio stream inspection returned malformed streams payload | media=%s | streams_type=%s",
            media_path,
            type(streams).__name__,
        )
        raise RuntimeError("ffprobe returned malformed audio stream metadata.")

    audio_streams: list[AudioStreamInfo] = []
    for position, stream in enumerate(streams, start=1):
        if not isinstance(stream, dict):
            logger.warning(
                "Skipping malformed ffprobe audio stream entry | media=%s | position=%s | entry_type=%s",
                media_path,
                position,
                type(stream).__name__,
            )
            continue
        stream_index = stream.get("index")
        if stream_index is None:
            continue
        try:
            audio_streams.append(
                AudioStreamInfo(
                    stream_index=int(stream_index),
                    label=_build_audio_stream_label(stream, position),
                    is_default=int((stream.get("disposition") or {}).get("default", 0) or 0) == 1,
                )
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "Skipping malformed ffprobe audio stream metadata | media=%s | position=%s | stream_index=%s | reason=%s",
                media_path,
                position,
                stream_index,
                exc,
            )
            continue

    return audio_streams


def probe_audio_streams(media_path: str) -> list[AudioStreamInfo]:
    command = build_audio_stream_probe_command(media_path)

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=FFPROBE_AUDIO_STREAM_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        logger.error(
            "ffprobe audio stream inspection timed out | media=%s | timeout_seconds=%s",
            media_path,
            FFPROBE_AUDIO_STREAM_TIMEOUT_SECONDS,
        )
        raise RuntimeError(
            "Audio stream inspection timed out after "
            f"{FFPROBE_AUDIO_STREAM_TIMEOUT_SECONDS:g} seconds."
        ) from exc
    except FileNotFoundError as exc:
        logger.error("ffprobe executable was not found during audio stream inspection | media=%s", media_path)
        raise RuntimeError("ffprobe was not found. Please install ffmpeg/ffprobe to inspect audio streams.") from exc
    except OSError as exc:
        logger.error(
            "ffprobe audio stream inspection could not be started | media=%s | reason=%s",
            media_path,
            exc,
        )
        raise RuntimeError(f"Audio stream inspection failed to start: {exc}") from exc

    if result.returncode != 0:
        error_text = (result.stderr or result.stdout or "Unknown ffprobe error.").strip()
        logger.error(
            "ffprobe audio stream inspection failed | media=%s | returncode=%s | details=%s",
            media_path,
            result.returncode,
            error_text,
        )
        raise RuntimeError(f"Failed to inspect audio streams: {error_text}")

    return parse_audio_stream_probe_output(media_path, result.stdout)
