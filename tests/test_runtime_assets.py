import pytest

from utils import runtime_assets


def test_resolve_whisper_model_reference_requires_local_model(monkeypatch, workspace_tmp_path):
    runtime_root = workspace_tmp_path / "runtime"
    writable_root = workspace_tmp_path / "programdata"
    monkeypatch.setenv("A1LPLAYER_RUNTIME_DIR", str(runtime_root))
    monkeypatch.setenv("A1LPLAYER_WRITABLE_RUNTIME_DIR", str(writable_root))

    with pytest.raises(FileNotFoundError, match="small"):
        runtime_assets.resolve_whisper_model_reference("small")

    model_dir = writable_root / "models" / "faster-whisper-small"
    model_dir.mkdir(parents=True)
    (model_dir / "model.bin").write_text("model", encoding="utf-8")

    assert runtime_assets.resolve_whisper_model_reference("small") == str(model_dir)


def test_closest_installed_weaker_whisper_model(monkeypatch, workspace_tmp_path):
    monkeypatch.setenv("A1LPLAYER_RUNTIME_DIR", str(workspace_tmp_path / "runtime"))
    monkeypatch.setenv("A1LPLAYER_WRITABLE_RUNTIME_DIR", str(workspace_tmp_path / "runtime"))

    small_dir = workspace_tmp_path / "runtime" / "models" / "faster-whisper-small"
    small_dir.mkdir(parents=True)
    (small_dir / "model.bin").write_text("model", encoding="utf-8")

    assert runtime_assets.closest_installed_weaker_whisper_model("large-v3") == "small"
    assert runtime_assets.closest_installed_weaker_whisper_model("tiny") is None
