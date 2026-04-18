import json


def test_legacy_helper_protocol_imports_still_build_shared_events():
    from services.runtime.RuntimeHelperProtocol import (
        EVENT_CANCELED,
        EVENT_FAILED,
        EVENT_FINISHED,
        build_canceled_event,
        build_failed_event,
    )

    assert EVENT_FINISHED == "finished"
    assert EVENT_FAILED == "failed"
    assert EVENT_CANCELED == "canceled"
    assert build_failed_event("x", "y") == {
        "event": "failed",
        "user_message": "x",
        "diagnostics": "y",
    }
    assert build_canceled_event() == {"event": "canceled"}


def test_legacy_installer_protocol_imports_still_build_shared_events():
    from services.runtime.RuntimeInstallerProtocol import (
        EVENT_CANCELED,
        EVENT_FAILED,
        EVENT_FINISHED,
        build_canceled_event,
        build_failed_event,
    )

    assert EVENT_FINISHED == "finished"
    assert EVENT_FAILED == "failed"
    assert EVENT_CANCELED == "canceled"
    assert build_failed_event("x", "y") == {
        "event": "failed",
        "user_message": "x",
        "diagnostics": "y",
    }
    assert build_canceled_event() == {"event": "canceled"}


def test_helper_specific_finished_event_payload_is_unchanged():
    from services.runtime.RuntimeHelperProtocol import build_finished_event

    assert build_finished_event(
        "C:/tmp/out.srt",
        True,
        used_fallback_output_path=True,
    ) == {
        "event": "finished",
        "output_path": "C:/tmp/out.srt",
        "auto_open": True,
        "used_fallback_output_path": True,
    }


def test_installer_specific_finished_event_payload_is_unchanged():
    from services.runtime.RuntimeInstallerProtocol import build_finished_event

    assert build_finished_event() == {"event": "finished"}


def test_runtime_request_json_roundtrips_are_unchanged():
    from services.runtime.RuntimeHelperProtocol import SubtitleGenerationRequest
    from services.runtime.RuntimeInstallerProtocol import CudaRuntimeInstallRequest

    subtitle_request = SubtitleGenerationRequest(
        media_path="C:/media/movie.mkv",
        audio_stream_index=2,
        audio_language="en",
        device="cuda",
        model_size="small",
        output_format="srt",
        output_path="C:/tmp/movie.srt",
        auto_open_after_generation=True,
    )
    subtitle_payload = json.loads(subtitle_request.to_json())
    assert subtitle_payload == {
        "media_path": "C:/media/movie.mkv",
        "audio_stream_index": 2,
        "audio_language": "en",
        "device": "cuda",
        "model_size": "small",
        "output_format": "srt",
        "output_path": "C:/tmp/movie.srt",
        "auto_open_after_generation": True,
    }
    assert SubtitleGenerationRequest.from_json(subtitle_request.to_json()) == subtitle_request

    installer_request = CudaRuntimeInstallRequest(
        packages=("nvidia-cuda-runtime-cu12", "nvidia-cublas-cu12"),
        install_target="C:/tmp/runtime",
    )
    installer_payload = json.loads(installer_request.to_json())
    assert installer_payload == {
        "packages": ["nvidia-cuda-runtime-cu12", "nvidia-cublas-cu12"],
        "install_target": "C:/tmp/runtime",
    }
    assert CudaRuntimeInstallRequest.from_json(installer_request.to_json()) == installer_request
