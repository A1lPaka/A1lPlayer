import importlib
import sys
import types


def test_import_mainwindow_has_no_runtime_side_effects(monkeypatch):
    installer_calls = []
    helper_calls = []

    installer_module = types.ModuleType("services.runtime.RuntimeInstallerMain")
    helper_module = types.ModuleType("services.runtime.RuntimeHelperMain")

    def _unexpected_installer_call(argv=None):
        installer_calls.append(argv)
        raise AssertionError("Runtime installer dispatch must not run during import")

    def _unexpected_helper_call(argv=None):
        helper_calls.append(argv)
        raise AssertionError("Runtime helper dispatch must not run during import")

    installer_module.try_run_runtime_installer = _unexpected_installer_call
    helper_module.try_run_runtime_helper = _unexpected_helper_call

    monkeypatch.setitem(sys.modules, "services.runtime.RuntimeInstallerMain", installer_module)
    monkeypatch.setitem(sys.modules, "services.runtime.RuntimeHelperMain", helper_module)
    monkeypatch.setattr(sys, "argv", ["python", "--helper", "subtitle-generation"])
    message_box_module = sys.modules.get("ui.MessageBoxService")
    if message_box_module is not None and not hasattr(message_box_module, "show_playback_error"):
        monkeypatch.setattr(message_box_module, "show_playback_error", lambda *_args, **_kwargs: None, raising=False)
    sys.modules.pop("MainWindow", None)

    module = importlib.import_module("MainWindow")

    assert module.main is not None
    assert installer_calls == []
    assert helper_calls == []
