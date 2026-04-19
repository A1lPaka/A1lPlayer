from utils.metrics import get_metrics


class _FakeGeometry:
    def width(self):
        return 1600

    def height(self):
        return 900


class _FakeScreen:
    def geometry(self):
        return _FakeGeometry()

    def devicePixelRatio(self):
        return 1.5


class _FakeHandle:
    def screen(self):
        return None


class _FakeWidget:
    def window(self):
        return self

    def windowHandle(self):
        return _FakeHandle()


def test_get_metrics_falls_back_to_primary_screen_when_window_handle_has_no_screen(monkeypatch):
    monkeypatch.setattr("utils.metrics.QGuiApplication.primaryScreen", lambda: _FakeScreen())

    metrics = get_metrics(_FakeWidget())

    assert metrics.min_window_side == 900
    assert metrics.scale_factor == 1.5
