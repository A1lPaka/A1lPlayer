from PySide6.QtCore import QPoint, QTimer, Qt
from PySide6.QtSvgWidgets import QSvgWidget

from utils import Metrics, res_path


class AnimatedVideoPlaceholder(QSvgWidget):
    IDLE_MS = 60000
    BOUNCE_INTERVAL_MS = 16
    RETURN_INTERVAL_MS = 12
    BOUNCE_STEP = 3
    RETURN_STEP = 40
    SIZE_MULTIPLIER = 7

    def __init__(self, parent, metrics: Metrics | None = None):
        super().__init__(res_path("assets/logo.svg"), parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        self.metrics = metrics
        self._velocity = QPoint(self.BOUNCE_STEP, self.BOUNCE_STEP)

        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.setInterval(self.IDLE_MS)
        self._idle_timer.timeout.connect(self._start_bounce)

        self._bounce_timer = QTimer(self)
        self._bounce_timer.setInterval(self.BOUNCE_INTERVAL_MS)
        self._bounce_timer.timeout.connect(self._move_bounce_step)

        self._return_timer = QTimer(self)
        self._return_timer.setInterval(self.RETURN_INTERVAL_MS)
        self._return_timer.timeout.connect(self._return_to_center_step)

    def apply_metrics(self, metrics: Metrics):
        self.metrics = metrics
        self.refresh_position()

    def show_placeholder(self):
        self.show()
        self._position_in_center()
        self._restart_idle_timer()

    def hide_placeholder(self):
        self._stop_motion()
        self.hide()

    def refresh_position(self):
        if not self.isVisible():
            return
        if self._bounce_timer.isActive():
            self._clamp_to_bounds()
            return
        self._position_in_center()

    def notify_activity(self):
        if not self.isVisible():
            return

        self._idle_timer.stop()
        self._bounce_timer.stop()

        if self.pos() == self._center_point():
            self._return_timer.stop()
            self._restart_idle_timer()
            return

        if not self._return_timer.isActive():
            self._return_timer.start()

    def _restart_idle_timer(self):
        if self.isVisible():
            self._idle_timer.start()

    def _stop_motion(self):
        self._idle_timer.stop()
        self._bounce_timer.stop()
        self._return_timer.stop()

    def _start_bounce(self):
        if not self.isVisible():
            return
        self._return_timer.stop()
        self._bounce_timer.start()

    def _move_bounce_step(self):
        if not self.isVisible():
            self._bounce_timer.stop()
            return

        max_x, max_y = self._bounds()
        x = self.x() + self._velocity.x()
        y = self.y() + self._velocity.y()
        vx = self._velocity.x()
        vy = self._velocity.y()

        if x <= 0 or x >= max_x:
            vx = -vx
            x = max(0, min(max_x, x))
        if y <= 0 or y >= max_y:
            vy = -vy
            y = max(0, min(max_y, y))

        self._velocity = QPoint(vx, vy)
        self.move(x, y)

    def _return_to_center_step(self):
        if not self.isVisible():
            self._return_timer.stop()
            return

        target = self._center_point()
        current = self.pos()
        dx = target.x() - current.x()
        dy = target.y() - current.y()

        if abs(dx) <= self.RETURN_STEP and abs(dy) <= self.RETURN_STEP:
            self.move(target)
            self._return_timer.stop()
            self._restart_idle_timer()
            return

        step_x = max(-self.RETURN_STEP, min(self.RETURN_STEP, dx))
        step_y = max(-self.RETURN_STEP, min(self.RETURN_STEP, dy))
        self.move(current.x() + step_x, current.y() + step_y)

    def _position_in_center(self):
        size = self._placeholder_size()
        center = self._center_point()
        self.setGeometry(center.x(), center.y(), size, size)

    def _clamp_to_bounds(self):
        size = self._placeholder_size()
        max_x, max_y = self._bounds(size)
        x = max(0, min(max_x, self.x()))
        y = max(0, min(max_y, self.y()))
        self.setGeometry(x, y, size, size)

    def _placeholder_size(self) -> int:
        parent = self.parentWidget()
        if parent is None:
            return 1

        base_icon_size = self.metrics.icon_size if self.metrics is not None else 32
        desired_size = max(1, base_icon_size * self.SIZE_MULTIPLIER)
        return max(1, min(desired_size, parent.width(), parent.height()))

    def _center_point(self) -> QPoint:
        parent = self.parentWidget()
        if parent is None:
            return QPoint(0, 0)

        size = self._placeholder_size()
        x = max(0, (parent.width() - size) // 2)
        y = max(0, (parent.height() - size) // 2)
        return QPoint(x, y)

    def _bounds(self, size: int | None = None) -> tuple[int, int]:
        parent = self.parentWidget()
        if parent is None:
            return 0, 0

        placeholder_size = self.width() if size is None else size
        max_x = max(0, parent.width() - placeholder_size)
        max_y = max(0, parent.height() - placeholder_size)
        return max_x, max_y
