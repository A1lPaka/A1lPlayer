from types import SimpleNamespace
import sys
import threading

import pytest

from services.subtitles.domain.SubtitleTypes import (
    SubtitleGenerationCanceledError,
    SubtitleGenerationEmptyResultError,
    SubtitleSegment,
)
from services.subtitles.io.AudioStreamProbe import parse_audio_stream_probe_output
from services.subtitles.io.SubtitleFileWriter import SubtitleFileWriter
from services.subtitles.io.SubtitleMaker import SubtitleMaker


def _not_canceled(_cancel_event, _context=None):
    return None


def test_audio_stream_probe_parses_ffprobe_json_with_labels():
    streams = parse_audio_stream_probe_output(
        "movie.mkv",
        """
        {
          "streams": [
            {
              "index": 1,
              "codec_name": "aac",
              "channels": 2,
              "channel_layout": "stereo",
              "tags": {"language": "eng", "title": "Main"},
              "disposition": {"default": 1}
            }
          ]
        }
        """,
    )

    assert streams[0].stream_index == 1
    assert streams[0].label == "Audio 1 | Main | ENG | stereo | AAC | default"
    assert streams[0].is_default is True


def test_audio_stream_probe_empty_stdout_returns_no_streams():
    assert parse_audio_stream_probe_output("silent.mp4", "") == []


def test_audio_stream_probe_rejects_invalid_json():
    with pytest.raises(RuntimeError, match="invalid audio stream metadata"):
        parse_audio_stream_probe_output("broken.mkv", "{nope")


def test_audio_stream_probe_stream_without_metadata_uses_generic_label():
    streams = parse_audio_stream_probe_output(
        "movie.mkv",
        '{"streams": [{"index": 3}]}',
    )

    assert streams[0].stream_index == 3
    assert streams[0].label == "Audio 1"
    assert streams[0].is_default is False


def test_subtitle_file_writer_saves_srt(workspace_tmp_path):
    output_path = workspace_tmp_path / "movie.srt"
    writer = SubtitleFileWriter(_not_canceled)

    saved_path = writer.save_srt(
        [
            SubtitleSegment(1.2, 3.456, "Hello"),
            SubtitleSegment(65.0, 66.25, "World"),
        ],
        str(output_path),
    )

    assert saved_path == str(output_path)
    assert output_path.read_text(encoding="utf-8") == (
        "1\n"
        "00:00:01,200 --> 00:00:03,456\n"
        "Hello\n\n"
        "2\n"
        "00:01:05,000 --> 00:01:06,250\n"
        "World\n\n"
    )


def test_subtitle_file_writer_uses_fallback_name_when_unconfirmed_output_appears(workspace_tmp_path):
    output_path = workspace_tmp_path / "movie.srt"
    output_path.write_text("existing", encoding="utf-8")
    writer = SubtitleFileWriter(_not_canceled)

    saved_path = writer.save_srt(
        [SubtitleSegment(0, 1, "Generated")],
        str(output_path),
        allow_unconfirmed_overwrite=False,
    )

    fallback_path = workspace_tmp_path / "movie (1).srt"
    assert saved_path == str(fallback_path)
    assert output_path.read_text(encoding="utf-8") == "existing"
    assert "Generated" in fallback_path.read_text(encoding="utf-8")


def test_subtitle_file_writer_empty_segments_creates_empty_srt(workspace_tmp_path):
    output_path = workspace_tmp_path / "empty.srt"
    writer = SubtitleFileWriter(_not_canceled)

    saved_path = writer.save_srt([], str(output_path))

    assert saved_path == str(output_path)
    assert output_path.read_text(encoding="utf-8") == ""


def test_subtitle_file_writer_canonicalizes_relative_output_path(workspace_tmp_path, monkeypatch):
    monkeypatch.chdir(workspace_tmp_path)
    writer = SubtitleFileWriter(_not_canceled)

    saved_path = writer.save_srt([], "nested/movie.srt")

    output_path = workspace_tmp_path / "nested" / "movie.srt"
    assert saved_path == str(output_path.resolve())
    assert output_path.is_file()


def test_subtitle_file_writer_reports_destination_folder_errors(workspace_tmp_path):
    parent_file = workspace_tmp_path / "not-a-folder"
    parent_file.write_text("file", encoding="utf-8")
    writer = SubtitleFileWriter(_not_canceled)

    with pytest.raises(RuntimeError, match="destination folder path points to a file"):
        writer.save_srt([SubtitleSegment(0, 1, "text")], str(parent_file / "movie.srt"))


def test_subtitle_maker_cancel_flow_stops_before_model_load(monkeypatch):
    maker = SubtitleMaker(model_size="tiny", device="cpu")
    cancel_event = threading.Event()
    cancel_event.set()

    monkeypatch.setattr(
        maker,
        "load_model",
        lambda: (_ for _ in ()).throw(AssertionError("model should not load after cancel")),
    )

    with pytest.raises(SubtitleGenerationCanceledError):
        maker.transcribe_file("movie.mp4", cancel_event=cancel_event)


def test_subtitle_maker_empty_result_raises(monkeypatch):
    maker = SubtitleMaker(model_size="tiny", device="cpu")

    class _EmptyModel:
        def transcribe(self, *_args, **_kwargs):
            return iter(()), SimpleNamespace(duration=10.0, language="en")

    monkeypatch.setattr(maker, "load_model", lambda: _EmptyModel())

    with pytest.raises(SubtitleGenerationEmptyResultError, match="No speech was detected"):
        maker.transcribe_file("movie.mp4")


def test_subtitle_maker_load_model_passes_model_device_and_compute_type(monkeypatch):
    calls = []

    class _WhisperModel:
        def __init__(self, model_size, *, device, compute_type):
            calls.append((model_size, device, compute_type))

    fake_module = SimpleNamespace(WhisperModel=_WhisperModel)
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)
    monkeypatch.setattr("services.subtitles.io.SubtitleMaker.configure_windows_nvidia_runtime_paths", lambda: None)
    monkeypatch.setattr("services.subtitles.io.SubtitleMaker.resolve_whisper_model_reference", lambda model: f"C:/runtime/{model}")
    monkeypatch.setattr(SubtitleMaker, "_detect_device", lambda self: "cuda")

    model = SubtitleMaker(model_size="medium", device="cuda").load_model()

    assert isinstance(model, _WhisperModel)
    assert calls == [("C:/runtime/medium", "cuda", "float16")]


def test_subtitle_maker_extracts_selected_audio_stream_and_transcribes_extracted_file(monkeypatch):
    maker = SubtitleMaker(model_size="small", device="cpu")
    calls = {"extract": [], "cleanup": [], "transcribe": []}

    class _Model:
        def transcribe(self, source_path, **kwargs):
            calls["transcribe"].append((source_path, kwargs))
            return iter([SimpleNamespace(start=0.0, end=2.0, text=" hello ")]), SimpleNamespace(
                duration=2.0,
                language="de",
            )

    monkeypatch.setattr(maker, "load_model", lambda: _Model())
    monkeypatch.setattr(
        maker,
        "_extract_audio_stream",
        lambda media_path, audio_stream_index, cancel_event=None: calls["extract"].append(
            (media_path, audio_stream_index, cancel_event)
        )
        or "extracted.wav",
    )
    monkeypatch.setattr(maker, "_remove_file_if_exists", lambda path: calls["cleanup"].append(path))

    segments = maker.transcribe_file("movie.mkv", audio_stream_index=5, language="de")

    assert segments == [SubtitleSegment(start=0.0, end=2.0, text="hello")]
    assert calls["extract"] == [("movie.mkv", 5, None)]
    assert calls["cleanup"] == ["extracted.wav"]
    assert calls["transcribe"] == [
        (
            "extracted.wav",
            {
                "language": "de",
                "task": "transcribe",
                "vad_filter": True,
            },
        )
    ]
