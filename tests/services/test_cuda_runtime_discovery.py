from services.subtitles.domain import CudaRuntimeDiscovery as discovery


def test_cuda_runtime_discovery_uses_managed_runtime_root(monkeypatch, workspace_tmp_path):
    cuda_root = workspace_tmp_path / "runtime" / "components" / "cuda"
    monkeypatch.setenv("A1LPLAYER_CUDA_TARGET", str(cuda_root))
    monkeypatch.setattr(discovery.os, "name", "nt", raising=False)

    for _package, relative_paths in discovery.WINDOWS_CUDA_RUNTIME_PACKAGE_FILES:
        for relative_path in relative_paths:
            dll_path = cuda_root / relative_path
            dll_path.parent.mkdir(parents=True, exist_ok=True)
            dll_path.write_text("dll", encoding="utf-8")

    assert discovery.get_missing_windows_cuda_runtime_packages() == []


def test_cuda_runtime_discovery_reports_missing_managed_runtime_package(monkeypatch, workspace_tmp_path):
    cuda_root = workspace_tmp_path / "runtime" / "components" / "cuda"
    monkeypatch.setenv("A1LPLAYER_CUDA_TARGET", str(cuda_root))
    monkeypatch.setattr(discovery.os, "name", "nt", raising=False)

    assert discovery.get_missing_windows_cuda_runtime_packages() == [
        package for package, _paths in discovery.WINDOWS_CUDA_RUNTIME_PACKAGE_FILES
    ]


def test_cuda_runtime_discovery_requires_pinned_metadata(monkeypatch, workspace_tmp_path):
    cuda_root = workspace_tmp_path / "runtime" / "components" / "cuda"
    monkeypatch.setenv("A1LPLAYER_CUDA_TARGET", str(cuda_root))
    monkeypatch.setattr(discovery.os, "name", "nt", raising=False)

    for _package, relative_paths in discovery.WINDOWS_CUDA_RUNTIME_PACKAGE_FILES:
        dll_path = cuda_root / relative_paths[0]
        dll_path.parent.mkdir(parents=True, exist_ok=True)
        dll_path.write_text("dll", encoding="utf-8")

    assert discovery.get_missing_windows_cuda_runtime_packages() == [
        package for package, _paths in discovery.WINDOWS_CUDA_RUNTIME_PACKAGE_FILES
    ]
