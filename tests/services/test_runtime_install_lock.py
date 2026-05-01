import pytest

from services.runtime import RuntimeInstallLock as install_lock


def test_runtime_install_lock_reports_busy_target(monkeypatch, workspace_tmp_path):
    monkeypatch.setattr(install_lock, "_try_lock_file", lambda _lock_file: False)

    with pytest.raises(install_lock.RuntimeInstallLockError) as exc_info:
        with install_lock.runtime_install_lock(workspace_tmp_path / "runtime" / "cuda", "CUDA runtime"):
            pass

    assert "Another CUDA runtime installation is already running" in str(exc_info.value)


def test_runtime_install_lock_failed_acquisition_keeps_existing_lock_file(monkeypatch, workspace_tmp_path):
    target = workspace_tmp_path / "runtime" / "cuda"
    target.parent.mkdir(parents=True)
    lock_path = target.with_name("cuda.install.lock")
    lock_path.write_text("held", encoding="utf-8")
    monkeypatch.setattr(install_lock, "_try_lock_file", lambda _lock_file: False)

    with pytest.raises(install_lock.RuntimeInstallLockError):
        with install_lock.runtime_install_lock(target, "CUDA runtime"):
            pass

    assert lock_path.read_text(encoding="utf-8") == "held"


def test_runtime_install_lock_blocks_second_holder_for_same_target(workspace_tmp_path):
    target = workspace_tmp_path / "runtime" / "cuda"

    with install_lock.runtime_install_lock(target, "CUDA runtime"):
        with pytest.raises(install_lock.RuntimeInstallLockError):
            with install_lock.runtime_install_lock(target, "CUDA runtime"):
                pass

    with install_lock.runtime_install_lock(target, "CUDA runtime"):
        pass


def test_runtime_install_lock_removes_lock_file_after_release(workspace_tmp_path):
    target = workspace_tmp_path / "runtime" / "cuda"
    lock_path = target.with_name("cuda.install.lock")

    with install_lock.runtime_install_lock(target, "CUDA runtime"):
        assert lock_path.is_file()

    assert lock_path.exists() is False
