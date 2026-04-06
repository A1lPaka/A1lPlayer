import json
import os

from utils import _color_from_state


class ThemeColor:
    DEFAULTS = {
        "text_color": (255, 255, 255),
        "panel_bg_color": (35, 35, 35),
        "control_button_color": (125, 125, 125),
        "volume_bar_color_active": (0, 40, 190),
        "progress_bar_color_active": (0, 40, 190),
        "time_popup_color": (255, 255, 255),
        "time_popup_text_color": (0, 0, 0),
    }
    DISPLAY_NAMES = {
        "text_color": "Text",
        "panel_bg_color": "Panel Background",
        "control_button_color": "Control Buttons",
        "volume_bar_color_active": "Volume Bar",
        "progress_bar_color_active": "Progress Bar",
        "time_popup_color": "Time Popup",
        "time_popup_text_color": "Time Popup Text",
    }
    DERIVED_COLOR_BUILDERS = {
        "panel_bg_color": (
            ("panel_bg_color_hovered", "hovered"),
            ("panel_bg_color_pressed", "pressed"),
            ("panel_bg_color_separator", "separator")
        ),
        "control_button_color": (
            ("control_button_color_hovered", "hovered"),
            ("control_button_color_pressed", "pressed"),
        ),
        "volume_bar_color_active": (
            ("volume_bar_color_inactive", "inactive"),
        ),
    }

    def __init__(self, colors: dict = None):
        self.colors = self.DEFAULTS.copy()
        if colors:
            self.colors.update(colors)
        self._update_derived_colors()

    def get(self, name):
        return self.colors.get(name)

    def set(self, name, value):
        canonical_name = self._canonical_name(name)
        self.colors[canonical_name] = value
        self._update_derived_colors(canonical_name)

    def save_to_file(self, path):
        base_colors = self.base_colors()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(base_colors, f)

    def base_colors(self) -> dict:
        return {k: v for k, v in self.colors.items() if k in self.DEFAULTS}

    def _update_derived_colors(self, source_name: str | None = None):
        if source_name is None:
            sources = self.DERIVED_COLOR_BUILDERS.keys()
        else:
            sources = (source_name,)

        for source in sources:
            for derived_name, state in self.DERIVED_COLOR_BUILDERS.get(source, ()):
                self.colors[derived_name] = _color_from_state(state, self.colors[source])

    def _canonical_name(self, name: str) -> str:
        return name

    @classmethod
    def load_from_file(cls, path):
        if not os.path.exists(path):
            return cls()
        with open(path, "r", encoding="utf-8") as f:
            colors = json.load(f)
        for k, v in colors.items():
            if isinstance(v, list):
                colors[k] = tuple(v)
        return cls(colors)
