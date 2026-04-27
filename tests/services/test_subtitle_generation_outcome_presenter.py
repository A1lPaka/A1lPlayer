from services.subtitles.presentation import SubtitleGenerationOutcomePresenter as presenter_module
from services.subtitles.presentation.SubtitleGenerationOutcomePresenter import (
    SubtitleAutoOpenOutcome,
    SubtitleGenerationOutcomePresenter,
)


def test_generation_success_routes_context_change(monkeypatch):
    calls = []
    monkeypatch.setattr(
        presenter_module,
        "show_subtitle_created_not_loaded_due_to_context_change",
        lambda parent, output: calls.append((parent, output)),
    )

    presenter = SubtitleGenerationOutcomePresenter("parent")
    presenter.show_generation_success(
        "C:/media/movie.srt",
        SubtitleAutoOpenOutcome.CONTEXT_CHANGED,
        used_fallback_output_path=False,
        requested_output_path=None,
    )

    assert calls == [("parent", "C:/media/movie.srt")]


def test_generation_success_routes_load_failed(monkeypatch):
    calls = []
    monkeypatch.setattr(
        presenter_module,
        "show_subtitle_auto_load_failed",
        lambda parent, output: calls.append((parent, output)),
    )

    presenter = SubtitleGenerationOutcomePresenter("parent")
    presenter.show_generation_success(
        "C:/media/movie.srt",
        SubtitleAutoOpenOutcome.LOAD_FAILED,
        used_fallback_output_path=False,
        requested_output_path=None,
    )

    assert calls == [("parent", "C:/media/movie.srt")]


def test_generation_success_routes_fallback_name(monkeypatch):
    calls = []
    monkeypatch.setattr(
        presenter_module,
        "show_subtitle_created_with_fallback_name",
        lambda parent, requested, output: calls.append((parent, requested, output)),
    )

    presenter = SubtitleGenerationOutcomePresenter("parent")
    presenter.show_generation_success(
        "C:/media/movie-2.srt",
        SubtitleAutoOpenOutcome.LOADED,
        used_fallback_output_path=True,
        requested_output_path="C:/media/movie.srt",
    )

    assert calls == [("parent", "C:/media/movie.srt", "C:/media/movie-2.srt")]


def test_generation_success_routes_normal_created(monkeypatch):
    calls = []
    monkeypatch.setattr(
        presenter_module,
        "show_subtitle_created",
        lambda parent, output: calls.append((parent, output)),
    )

    presenter = SubtitleGenerationOutcomePresenter("parent")
    presenter.show_generation_success(
        "C:/media/movie.srt",
        SubtitleAutoOpenOutcome.LOADED,
        used_fallback_output_path=False,
        requested_output_path=None,
    )

    assert calls == [("parent", "C:/media/movie.srt")]


def test_failure_and_cancel_routes(monkeypatch):
    calls = []
    monkeypatch.setattr(
        presenter_module,
        "show_subtitle_generation_failed",
        lambda parent, error: calls.append(("subtitle_failed", parent, error)),
    )
    monkeypatch.setattr(
        presenter_module,
        "show_subtitle_generation_canceled",
        lambda parent: calls.append(("subtitle_canceled", parent)),
    )
    monkeypatch.setattr(
        presenter_module,
        "show_cuda_runtime_install_failed",
        lambda parent, error: calls.append(("cuda_failed", parent, error)),
    )
    monkeypatch.setattr(
        presenter_module,
        "show_cuda_runtime_install_canceled",
        lambda parent: calls.append(("cuda_canceled", parent)),
    )

    presenter = SubtitleGenerationOutcomePresenter("parent")
    presenter.show_generation_failed("boom")
    presenter.show_generation_canceled()
    presenter.show_cuda_runtime_install_failed("missing runtime")
    presenter.show_cuda_runtime_install_canceled()

    assert calls == [
        ("subtitle_failed", "parent", "boom"),
        ("subtitle_canceled", "parent"),
        ("cuda_failed", "parent", "missing runtime"),
        ("cuda_canceled", "parent"),
    ]
