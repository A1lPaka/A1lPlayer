import threading

from services.app.AppTempService import AppTempService


def test_startup_cleanup_guard_is_thread_safe(monkeypatch, workspace_tmp_path):
    class _CleanupProbe:
        def __init__(self):
            self.cleaned_dirs = []
            self.lock = threading.Lock()

        def cleanup_owned_dir(self, owned_dir, _now):
            with self.lock:
                self.cleaned_dirs.append(owned_dir)

    parties = 4
    barrier = threading.Barrier(parties)
    probe = _CleanupProbe()

    monkeypatch.setattr(AppTempService, "_startup_cleanup_ran", False)
    monkeypatch.setattr(AppTempService, "get_app_temp_root", lambda: workspace_tmp_path)
    monkeypatch.setattr(AppTempService, "get_runtime_subtitles_dir", lambda: workspace_tmp_path / "runtime-subtitles")
    monkeypatch.setattr(AppTempService, "get_subtitle_generation_dir", lambda: workspace_tmp_path / "subtitle-generation")
    monkeypatch.setattr(AppTempService, "_cleanup_owned_dir", probe.cleanup_owned_dir)

    def cleanup():
        barrier.wait(timeout=5)
        AppTempService.cleanup_startup_orphans()

    threads = [threading.Thread(target=cleanup) for _ in range(parties)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert probe.cleaned_dirs == [
        workspace_tmp_path / "runtime-subtitles",
        workspace_tmp_path / "subtitle-generation",
    ]
