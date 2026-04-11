from __future__ import annotations

import json
import logging
import signal
import sys
import threading

from services.RuntimeExecution import get_runtime_mode_label
from services.RuntimeHelperProtocol import (
    HELPER_SUBTITLE_GENERATION,
    SubtitleGenerationRequest,
    build_canceled_event,
    build_failed_event,
    build_finished_event,
    build_progress_event,
)
from services.SubtitleMaker import (
    SubtitleGenerationCanceledError,
    SubtitleGenerationEmptyResultError,
    SubtitleMaker,
)
from utils.LoggingSetup import configure_logging


logger = logging.getLogger(__name__)


def _configure_stdio_utf8():
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def try_run_runtime_helper(argv: list[str] | None = None) -> int | None:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2 or args[0] != "--helper":
        return None

    helper_name = str(args[1]).strip().lower()
    _configure_stdio_utf8()
    configure_logging()
    logger.info(
        "Runtime helper mode activated | helper=%s | runtime_mode=%s | argv=%s | stdin_encoding=%s | stdout_encoding=%s",
        helper_name,
        get_runtime_mode_label(),
        args,
        getattr(sys.stdin, "encoding", None),
        getattr(sys.stdout, "encoding", None),
    )

    if helper_name == HELPER_SUBTITLE_GENERATION:
        return run_subtitle_generation_helper()

    logger.error("Unknown runtime helper requested | helper=%s", helper_name)
    sys.stderr.write(f"Unknown helper: {helper_name}\n")
    sys.stderr.flush()
    return 64


def _emit_event(event: dict):
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _read_stdin_payload() -> str:
    payload = sys.stdin.read().strip()
    if not payload:
        raise RuntimeError("Helper request payload is missing.")
    return payload


def _install_subtitle_signal_handlers(cancel_event: threading.Event, maker_ref: dict[str, SubtitleMaker | None]):
    def _handle_signal(signum, _frame):
        logger.warning("Subtitle generation helper received termination signal | signal=%s", signum)
        cancel_event.set()
        maker = maker_ref.get("maker")
        if maker is not None:
            maker.cancel()

    for signal_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signal_value = getattr(signal, signal_name, None)
        if signal_value is None:
            continue
        try:
            signal.signal(signal_value, _handle_signal)
        except (OSError, RuntimeError, ValueError):
            continue


def _build_subtitle_diagnostics(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _build_subtitle_user_message(exc: BaseException) -> str:
    if isinstance(exc, SubtitleGenerationEmptyResultError):
        return str(exc)
    if isinstance(exc, RuntimeError):
        return str(exc)
    return "Subtitle generation failed unexpectedly. See application logs for details."


def run_subtitle_generation_helper() -> int:
    cancel_event = threading.Event()
    maker_ref: dict[str, SubtitleMaker | None] = {"maker": None}
    _install_subtitle_signal_handlers(cancel_event, maker_ref)

    request: SubtitleGenerationRequest | None = None
    try:
        request = SubtitleGenerationRequest.from_json(_read_stdin_payload())
        logger.info(
            "Subtitle generation helper started | runtime_mode=%s | media=%s | output=%s | model=%s | requested_device=%s | audio_stream_index=%s | language=%s",
            get_runtime_mode_label(),
            request.media_path,
            request.output_path,
            request.model_size,
            request.device or "auto",
            request.audio_stream_index,
            request.audio_language or "auto",
        )

        maker = SubtitleMaker(
            model_size=request.model_size,
            device=request.device,
        )
        maker_ref["maker"] = maker

        def _progress_callback(status: str, progress: int, details: str):
            if cancel_event.is_set():
                raise SubtitleGenerationCanceledError()
            _emit_event(build_progress_event(status, progress, details))

        segments = maker.transcribe_file(
            request.media_path,
            audio_stream_index=request.audio_stream_index,
            language=request.audio_language,
            progress_callback=_progress_callback,
            cancel_event=cancel_event,
        )
        maker.save_subtitles(
            segments,
            request.output_path,
            request.output_format,
            cancel_event=cancel_event,
        )
        _emit_event(
            build_finished_event(
                request.output_path,
                request.auto_open_after_generation,
            )
        )
        logger.info(
            "Subtitle generation helper finished successfully | media=%s | output=%s | actual_device=%s",
            request.media_path,
            request.output_path,
            maker.device,
        )
        return 0
    except SubtitleGenerationCanceledError:
        logger.info(
            "Subtitle generation helper canceled | media=%s",
            request.media_path if request is not None else "<unknown>",
        )
        _emit_event(build_canceled_event())
        return 2
    except Exception as exc:
        logger.exception(
            "Subtitle generation helper failed | media=%s | output=%s",
            request.media_path if request is not None else "<unknown>",
            request.output_path if request is not None else "<unknown>",
        )
        _emit_event(build_failed_event(_build_subtitle_user_message(exc), _build_subtitle_diagnostics(exc)))
        return 1
    finally:
        maker = maker_ref.get("maker")
        if maker is not None:
            maker.cancel()
        maker_ref["maker"] = None
