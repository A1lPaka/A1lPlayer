from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from controllers.PlayerPlaybackController import PlayerPlaybackController


class PlayerActionsController(QObject):
    open_file_requested = Signal()
    fullscreen_requested = Signal()
    pip_requested = Signal()
    pip_exit_requested = Signal()

    def __init__(
        self,
        playback: PlayerPlaybackController,
        *,
        is_pip_active,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.playback = playback
        self._is_pip_active = is_pip_active

    def on_play_pause(self):
        if not self.playback.has_assigned_media():
            self.open_file_requested.emit()
            return
        self.playback.toggle_play_pause()

    def on_stop(self):
        self.playback.stop()
        if self._is_pip_active():
            self.pip_exit_requested.emit()

    def on_fullscreen(self):
        self.fullscreen_requested.emit()

    def on_pip(self):
        self.pip_requested.emit()

    def on_prev(self):
        self.playback.play_previous()

    def on_next(self):
        self.playback.play_next()

    def on_seek_hold(self, direction: str):
        self.playback.seek_by_hold(direction)

    def on_seek_started(self):
        self.playback.begin_seek()

    def on_seek(self, value: float):
        self.playback.seek_to_ratio(value)

    def on_seek_finished(self):
        self.playback.finish_seek()

    def on_speed_changed(self, speed: float) -> float:
        self.playback.set_rate(speed)
        return self.playback.get_rate()

    def adjust_speed(self, delta: float) -> float:
        current_speed = self.playback.get_rate()
        target_speed = max(0.25, min(4.0, current_speed + float(delta)))
        self.playback.set_rate(target_speed)
        return self.playback.get_rate()

    def reset_speed(self) -> float:
        self.playback.set_rate(1.0)
        return self.playback.get_rate()

    def on_volume_changed(self, volume: int) -> tuple[int, bool]:
        self.playback.set_volume(volume)
        return self.playback.get_desired_volume(), self.playback.is_muted()

    def adjust_volume(self, delta_percent: int) -> tuple[int, bool]:
        return self.on_volume_changed(self.playback.get_desired_volume() + int(delta_percent))

    def on_mute(self) -> tuple[int, bool]:
        self.playback.toggle_mute()
        return self.playback.get_desired_volume(), self.playback.is_muted()

    def seek_by_ms(self, delta_ms: int):
        self.playback.seek_by_ms(delta_ms)
