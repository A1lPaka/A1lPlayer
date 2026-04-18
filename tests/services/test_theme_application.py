from PySide6.QtGui import QColor
from PySide6.QtWidgets import QWidget

from models.ThemeColor import ThemeState
from ui.ColorThemeDialog import InterfacePreview
from ui.PiPWindow import PiPWindow
from ui.PlayerControls import PlayerControls
from ui.ThemeApplication import bar_theme, button_theme, theme_qcolor, theme_rgb
from utils import Metrics


class _FakeThemeWithDefault:
    def __init__(self, values=None):
        self._values = dict(values or {})

    def get(self, name, default=None):
        return self._values.get(name, default)


class _FakeThemeOneArg:
    def __init__(self, values=None):
        self._values = dict(values or {})

    def get(self, name):
        return self._values.get(name)


def _metrics() -> Metrics:
    return Metrics(
        min_window_side=900,
        scale_factor=1.0,
        window_width=720,
        window_height=450,
        icon_size=24,
        font_size=16,
        menu_width=90,
        theme_dialog_width=450,
        theme_dialog_height=514,
        pip_min_width=173,
        subtitle_dialog_width=450,
        subtitle_dialog_height=270,
        subtitle_progress_dialog_width=378,
        subtitle_progress_dialog_height=180,
    )


def test_theme_rgb_and_qcolor_read_theme_state_values():
    theme = ThemeState({"text_color": (1, 2, 3)})

    assert theme_rgb(theme, "text_color") == (1, 2, 3)
    color = theme_qcolor(theme, "text_color")

    assert isinstance(color, QColor)
    assert (color.red(), color.green(), color.blue()) == (1, 2, 3)


def test_theme_rgb_uses_fallback_for_missing_or_invalid_values():
    assert theme_rgb(_FakeThemeWithDefault(), "missing", (4, 5, 6)) == (4, 5, 6)
    assert theme_rgb(_FakeThemeWithDefault({"text_color": "bad"}), "text_color", (7, 8, 9)) == (7, 8, 9)
    assert theme_rgb(_FakeThemeOneArg({"text_color": [10, 11, 12]}), "text_color") == (10, 11, 12)


def test_button_and_bar_theme_use_expected_keys():
    theme = ThemeState(
        {
            "control_button_color": (10, 20, 30),
            "volume_bar_color_active": (40, 50, 60),
        }
    )

    buttons = button_theme(theme)
    volume = bar_theme(theme, "volume_bar_color_active", "volume_bar_color_inactive")

    assert buttons.normal == (10, 20, 30)
    assert buttons.hovered == theme.get("control_button_color_hovered")
    assert buttons.pressed == theme.get("control_button_color_pressed")
    assert volume.active == (40, 50, 60)
    assert volume.inactive == theme.get("volume_bar_color_inactive")


def test_player_controls_apply_theme_updates_internal_control_colors():
    theme = ThemeState(
        {
            "control_button_color": (11, 22, 33),
            "volume_bar_color_active": (44, 55, 66),
            "progress_bar_color_active": (77, 88, 99),
        }
    )
    parent = QWidget()
    controls = PlayerControls(parent, _metrics(), theme)
    controls.apply_theme(theme)

    assert controls.play_pause_button.bg_color == (11, 22, 33)
    assert controls.speed_button.bg_color_hovered == theme.get("control_button_color_hovered")
    assert controls.volume_controls.volume_bar.active_bg_color == (44, 55, 66)
    assert controls.volume_controls.volume_bar.inactive_bg_color == theme.get("volume_bar_color_inactive")
    assert controls.progress_bar.active_bg_color == (77, 88, 99)
    assert controls.progress_bar.inactive_bg_color == (11, 22, 33)


def test_interface_preview_uses_shared_control_and_bar_colors():
    theme = ThemeState(
        {
            "control_button_color": (12, 24, 36),
            "volume_bar_color_active": (48, 60, 72),
            "progress_bar_color_active": (84, 96, 108),
            "time_popup_color": (120, 132, 144),
        }
    )
    parent = QWidget()
    preview = InterfacePreview(theme, _metrics(), parent)
    preview.update_theme(theme)

    assert preview.play_button.bg_color == (12, 24, 36)
    assert preview.time_popup.bg_color == (120, 132, 144)
    assert preview.time_popup.bg_color_hovered == (120, 132, 144)
    assert preview.volume_controls.volume_bar.active_bg_color == (48, 60, 72)
    assert preview.progress_bar.active_bg_color == (84, 96, 108)


def test_pip_window_apply_theme_keeps_close_icon_available():
    pip_window = PiPWindow(_metrics(), ThemeState({"text_color": (15, 25, 35)}))
    pip_window.resize(320, 180)
    pip_window.apply_theme(pip_window.theme_color)

    assert not pip_window._close_button.icon().isNull()
