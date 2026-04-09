from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QEvent, QPoint, QRect, QPropertyAnimation, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QPushButton, QStyle, QWidget
from shiboken6 import isValid

from models.ThemeColor import ThemeState
from utils import Metrics


class PiPWindow(QWidget):
    closed = Signal()

    _FRAME_COLOR = QColor(102, 102, 102, 180)
    _FRAME_VISIBLE = 1
    _RESIZE_MARGIN = 10
    _TITLE_HEIGHT = 24
    _TITLE_PADDING = 4
    _TITLE_HIDE_DELAY_MS = 2000
    _TITLE_ANIMATION_MS = 180
    _MIN_CONTENT_WIDTH = 240
    _MIN_CONTENT_HEIGHT = 135

    _EDGE_NONE = 0
    _EDGE_LEFT = 1
    _EDGE_TOP = 2
    _EDGE_RIGHT = 4
    _EDGE_BOTTOM = 8

    def __init__(self, metrics: Metrics, theme_color: ThemeState):
        super().__init__()
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.setObjectName("pipWindow")
        self.setWindowTitle("A1lPlayer PiP")

        self.metrics = metrics
        self.theme_color = theme_color
        self._aspect_ratio: float | None = None
        self._central_widget: QWidget | None = None
        self._active_edges = self._EDGE_NONE
        self._drag_candidate = False
        self._dragging = False
        self._drag_offset = QPoint()
        self._press_global_pos = QPoint()
        self._press_geometry = QRect()
        self._resize_guard = False
        self._title_visible = True

        self._close_button = QPushButton(self)
        self._close_button.setObjectName("pipCloseButton")
        self._close_button.setCursor(Qt.ArrowCursor)
        self._close_button.setFlat(True)
        self._close_button.clicked.connect(self.close)

        self._content_host = QWidget(self)
        self._content_host.setObjectName("pipContentHost")

        self._title_hide_timer = QTimer(self)
        self._title_hide_timer.setSingleShot(True)
        self._title_hide_timer.setInterval(self._TITLE_HIDE_DELAY_MS)
        self._title_hide_timer.timeout.connect(self._hide_title_overlay)

        self._title_animation = QPropertyAnimation(self._close_button, b"pos", self)
        self._title_animation.setDuration(self._TITLE_ANIMATION_MS)
        self._title_animation.setEasingCurve(QEasingCurve.OutCubic)

        self._apply_style()
        self._install_interaction_filter(self)
        self._install_interaction_filter(self._content_host)
        self._apply_initial_size()
        self._update_minimum_size()
        self.apply_metrics(metrics)
        self.apply_theme(theme_color)
        self._set_title_overlay_visible(True, animate=False)

    def setCentralWidget(self, widget: QWidget):
        if self._central_widget is widget:
            return
        if self._central_widget is not None:
            self.takeCentralWidget()

        self._central_widget = widget
        widget.setParent(self._content_host)
        widget.show()

        self._install_interaction_filter(widget)
        for child in widget.findChildren(QWidget):
            self._install_interaction_filter(child)

        self._layout_children()

    def takeCentralWidget(self) -> QWidget | None:
        widget = self._central_widget
        if widget is None:
            return None
        self._central_widget = None
        widget.setParent(None)
        self._layout_children()
        return widget

    def centralWidget(self) -> QWidget | None:
        return self._central_widget

    def apply_metrics(self, metrics: Metrics):
        self.metrics = metrics
        self._TITLE_HEIGHT = max(1, int(metrics.icon_size * 1.35))
        self._TITLE_PADDING = max(1, int(metrics.scale_factor * 2))
        self._RESIZE_MARGIN = max(1, int(metrics.scale_factor * 6))
        self._layout_children()
        self._update_minimum_size()

    def apply_theme(self, theme_color: ThemeState):
        self.theme_color = theme_color
        self._apply_style()
        self._update_close_button_icon()

    def set_video_aspect_ratio(self, width: int, height: int):
        width = int(width)
        height = int(height)
        if width <= 0 or height <= 0:
            return

        self._aspect_ratio = width / height
        self._update_minimum_size()
        self._apply_aspect_ratio_to_window_size(self.width(), self.height())

    def closeEvent(self, event):
        self.closed.emit()
        event.ignore()
        self.hide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_children()
        if self._resize_guard or self._aspect_ratio is None:
            return
        self._apply_aspect_ratio_to_window_size(
            event.size().width(),
            event.size().height(),
            event.oldSize().width(),
            event.oldSize().height(),
        )

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing, False)
            painter.setPen(QPen(self._FRAME_COLOR, 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        finally:
            painter.end()

    def leaveEvent(self, event: QEvent):
        if self._active_edges == self._EDGE_NONE and not self._dragging:
            self.unsetCursor()
        super().leaveEvent(event)

    def eventFilter(self, watched, event):
        if watched is None or not isValid(watched):
            return False
        if not isinstance(watched, QWidget):
            return super().eventFilter(watched, event)
        if watched is self._close_button:
            return super().eventFilter(watched, event)
        if watched is not self and not self.isAncestorOf(watched):
            return super().eventFilter(watched, event)

        if event.type() in (QEvent.Enter, QEvent.MouseMove):
            self._show_title_overlay_temporarily()
        if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            return self._handle_press(watched, event)
        if event.type() == QEvent.MouseMove:
            return self._handle_move(watched, event)
        if event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
            return self._handle_release(watched, event)
        if event.type() == QEvent.Leave and self._active_edges == self._EDGE_NONE and not self._dragging:
            self.unsetCursor()
            self._title_hide_timer.start()

        return super().eventFilter(watched, event)

    def _handle_press(self, watched: QWidget, event) -> bool:
        local_pos = self._event_pos_in_window(watched, event)
        edges = self._edges_at(local_pos)
        if edges != self._EDGE_NONE:
            self._active_edges = edges
            self._drag_candidate = False
            self._dragging = False
            self._press_global_pos = event.globalPosition().toPoint()
            self._press_geometry = self.geometry()
            event.accept()
            return True

        self._drag_candidate = True
        self._dragging = False
        self._press_global_pos = event.globalPosition().toPoint()
        self._drag_offset = self._press_global_pos - self.frameGeometry().topLeft()
        return False

    def _handle_move(self, watched: QWidget, event) -> bool:
        local_pos = self._event_pos_in_window(watched, event)
        global_pos = event.globalPosition().toPoint()

        if self._active_edges != self._EDGE_NONE:
            self._perform_resize(global_pos)
            event.accept()
            return True
        if event.buttons() & Qt.LeftButton and self._drag_candidate:
            if not self._dragging:
                drag_distance = (global_pos - self._press_global_pos).manhattanLength()
                if drag_distance < QApplication.startDragDistance():
                    self._update_cursor(local_pos)
                    return False
                self._dragging = True
            self.move(global_pos - self._drag_offset)
            event.accept()
            return True
        if self._dragging and event.buttons() & Qt.LeftButton:
            self.move(global_pos - self._drag_offset)
            event.accept()
            return True

        self._update_cursor(local_pos)
        return False

    def _handle_release(self, watched: QWidget, event) -> bool:
        was_dragging = self._dragging
        was_resizing = self._active_edges != self._EDGE_NONE
        self._active_edges = self._EDGE_NONE
        self._drag_candidate = False
        self._dragging = False

        if not was_resizing and not was_dragging:
            return False
        self._show_title_overlay_temporarily()
        self._update_cursor(self._event_pos_in_window(watched, event))
        event.accept()
        return True

    def _layout_children(self):
        width = self.width()
        height = self.height()

        button_size = max(16, self._TITLE_HEIGHT - self._TITLE_PADDING * 2)
        self._close_button.setGeometry(
            width - self._FRAME_VISIBLE - button_size,
            self._FRAME_VISIBLE,
            button_size,
            button_size,
        )
        self._update_close_button_icon()

        content_rect = QRect(
            self._FRAME_VISIBLE,
            self._FRAME_VISIBLE,
            max(1, width - self._FRAME_VISIBLE * 2),
            max(1, height - self._FRAME_VISIBLE * 2),
        )
        self._content_host.setGeometry(content_rect)
        if self._central_widget is not None:
            self._central_widget.setGeometry(0, 0, content_rect.width(), content_rect.height())
        self._close_button.raise_()
        if self._title_animation.state() != QPropertyAnimation.Running:
            self._close_button.move(
                width - self._FRAME_VISIBLE - button_size,
                self._FRAME_VISIBLE if self._title_visible else -button_size,
            )

    def _frame_extra(self) -> tuple[int, int]:
        return self._FRAME_VISIBLE * 2, self._FRAME_VISIBLE * 2

    def _update_minimum_size(self):
        extra_width, extra_height = self._frame_extra()
        min_content_width = self._minimum_content_width()
        min_content_height = self._MIN_CONTENT_HEIGHT
        if self._aspect_ratio is not None:
            min_content_height = max(min_content_height, int(round(min_content_width / self._aspect_ratio)))
        self.setMinimumSize(min_content_width + extra_width, min_content_height + extra_height)

    def _apply_aspect_ratio_to_window_size(self, width: int, height: int, old_width: int = -1, old_height: int = -1):
        extra_width, extra_height = self._frame_extra()
        width = max(self.minimumWidth(), int(width))
        height = max(self.minimumHeight(), int(height))
        content_width = max(1, width - extra_width)
        content_height = max(1, height - extra_height)

        width_is_driver = old_width <= 0 or old_height <= 0 or abs(width - old_width) >= abs(height - old_height)
        if width_is_driver:
            target_content_width = content_width
            target_content_height = max(1, int(round(target_content_width / self._aspect_ratio)))
        else:
            target_content_height = content_height
            target_content_width = max(1, int(round(target_content_height * self._aspect_ratio)))

        target_width = max(self.minimumWidth(), target_content_width + extra_width)
        target_height = max(self.minimumHeight(), target_content_height + extra_height)
        if target_width == self.width() and target_height == self.height():
            return

        self._resize_guard = True
        self.resize(target_width, target_height)
        self._resize_guard = False

    def _perform_resize(self, global_pos: QPoint):
        press = self._press_geometry
        press_left = press.x()
        press_top = press.y()
        press_right = press_left + press.width()
        press_bottom = press_top + press.height()

        width = press.width()
        height = press.height()
        if self._active_edges & self._EDGE_LEFT:
            width = max(self.minimumWidth(), press_right - global_pos.x())
        elif self._active_edges & self._EDGE_RIGHT:
            width = max(self.minimumWidth(), global_pos.x() - press_left)

        if self._active_edges & self._EDGE_TOP:
            height = max(self.minimumHeight(), press_bottom - global_pos.y())
        elif self._active_edges & self._EDGE_BOTTOM:
            height = max(self.minimumHeight(), global_pos.y() - press_top)

        target_width, target_height = self._target_size_for_resize(width, height, global_pos - self._press_global_pos)
        left, top = self._anchored_position(target_width, target_height, press_left, press_top, press_right, press_bottom)
        self.setGeometry(QRect(left, top, target_width, target_height))

    def _target_size_for_resize(self, width: int, height: int, delta: QPoint) -> tuple[int, int]:
        width = max(self.minimumWidth(), int(width))
        height = max(self.minimumHeight(), int(height))
        if self._aspect_ratio is None:
            return width, height

        extra_width, extra_height = self._frame_extra()
        content_width = max(1, width - extra_width)
        content_height = max(1, height - extra_height)
        horizontal_resize = bool(self._active_edges & (self._EDGE_LEFT | self._EDGE_RIGHT))
        vertical_resize = bool(self._active_edges & (self._EDGE_TOP | self._EDGE_BOTTOM))

        if horizontal_resize and vertical_resize:
            press = self._press_geometry
            width_is_driver = abs(delta.x()) / max(1, press.width()) >= abs(delta.y()) / max(1, press.height())
        else:
            width_is_driver = horizontal_resize or not vertical_resize

        if width_is_driver:
            target_content_width = content_width
            target_content_height = max(1, int(round(target_content_width / self._aspect_ratio)))
        else:
            target_content_height = content_height
            target_content_width = max(1, int(round(target_content_height * self._aspect_ratio)))

        return (
            max(self.minimumWidth(), target_content_width + extra_width),
            max(self.minimumHeight(), target_content_height + extra_height),
        )

    def _anchored_position(
        self,
        target_width: int,
        target_height: int,
        press_left: int,
        press_top: int,
        press_right: int,
        press_bottom: int,
    ) -> tuple[int, int]:
        if self._active_edges & self._EDGE_LEFT:
            return press_right - target_width, press_bottom - target_height
        if self._active_edges & self._EDGE_TOP:
            return press_left, press_bottom - target_height
        return press_left, press_top

    def _edges_at(self, pos: QPoint) -> int:
        x = pos.x()
        y = pos.y()
        width = self.width()
        height = self.height()

        edges = self._EDGE_NONE
        if x <= self._RESIZE_MARGIN:
            edges |= self._EDGE_LEFT
        elif x >= width - self._RESIZE_MARGIN:
            edges |= self._EDGE_RIGHT

        if y <= self._RESIZE_MARGIN:
            edges |= self._EDGE_TOP
        elif y >= height - self._RESIZE_MARGIN:
            edges |= self._EDGE_BOTTOM

        return edges

    def _update_cursor(self, pos: QPoint):
        edges = self._edges_at(pos)
        if edges in (self._EDGE_LEFT, self._EDGE_RIGHT):
            self.setCursor(Qt.SizeHorCursor)
        elif edges in (self._EDGE_TOP, self._EDGE_BOTTOM):
            self.setCursor(Qt.SizeVerCursor)
        elif edges in (self._EDGE_TOP | self._EDGE_LEFT, self._EDGE_BOTTOM | self._EDGE_RIGHT):
            self.setCursor(Qt.SizeFDiagCursor)
        elif edges in (self._EDGE_TOP | self._EDGE_RIGHT, self._EDGE_BOTTOM | self._EDGE_LEFT):
            self.setCursor(Qt.SizeBDiagCursor)
        else:
            self.unsetCursor()

    def _apply_style(self):
        panel_bg = self._theme_tuple("panel_bg_color", (35, 35, 35))
        panel_bg_hovered = self._theme_tuple("panel_bg_color_hovered", panel_bg)
        self.setStyleSheet(
            f"""
            QWidget#pipWindow {{ background-color: rgb(14, 14, 14); border: none; }}
            QWidget#pipContentHost {{ background-color: rgb(0, 0, 0); }}
            QPushButton#pipCloseButton {{
                background-color: rgb({panel_bg[0]}, {panel_bg[1]}, {panel_bg[2]});
                border: none;
                border-radius: 0;
                padding: 0;
            }}
            QPushButton#pipCloseButton:hover {{
                background-color: rgb({panel_bg_hovered[0]}, {panel_bg_hovered[1]}, {panel_bg_hovered[2]});
            }}
            """
        )

    def _install_interaction_filter(self, widget: QWidget):
        widget.setMouseTracking(True)
        widget.installEventFilter(self)

    def _event_pos_in_window(self, watched: QWidget, event) -> QPoint:
        return self.mapFromGlobal(watched.mapToGlobal(event.position().toPoint()))

    def _show_title_overlay_temporarily(self):
        self._title_hide_timer.start()
        self._set_title_overlay_visible(True, animate=True)

    def _hide_title_overlay(self):
        if self._dragging:
            self._title_hide_timer.start()
            return
        self._set_title_overlay_visible(False, animate=True)

    def _set_title_overlay_visible(self, visible: bool, *, animate: bool):
        self._title_visible = bool(visible)
        self._title_animation.stop()
        button_size = self._close_button.height() or max(16, self._TITLE_HEIGHT - self._TITLE_PADDING * 2)
        target_pos = QPoint(
            self.width() - self._FRAME_VISIBLE - button_size,
            self._FRAME_VISIBLE if self._title_visible else -button_size,
        )
        if not animate:
            self._close_button.move(target_pos)
            return
        self._title_animation.setStartValue(self._close_button.pos())
        self._title_animation.setEndValue(target_pos)
        self._title_animation.start()

    def _theme_tuple(self, name: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
        color = self.theme_color.get(name)
        if isinstance(color, (tuple, list)) and len(color) >= 3:
            return int(color[0]), int(color[1]), int(color[2])
        return fallback

    def _minimum_content_width(self) -> int:
        return max(self._MIN_CONTENT_WIDTH, int(self.metrics.pip_min_width))

    def _apply_initial_size(self):
        self.resize(self.metrics.window_width // 2, self.metrics.window_height // 2)
        self.setMinimumWidth(self.metrics.pip_min_width)

    def _update_close_button_icon(self):
        button_size = min(self._close_button.width(), self._close_button.height())
        if button_size <= 0:
            return

        icon_size = max(8, button_size - max(6, self._TITLE_PADDING * 2))
        base_icon = self.style().standardIcon(QStyle.SP_TitleBarCloseButton)
        base_pixmap = base_icon.pixmap(icon_size, icon_size)
        if base_pixmap.isNull():
            return

        text_color = QColor(*self._theme_tuple("text_color", (255, 255, 255)))
        tinted_pixmap = QPixmap(base_pixmap.size())
        tinted_pixmap.fill(Qt.transparent)

        painter = QPainter(tinted_pixmap)
        painter.drawPixmap(0, 0, base_pixmap)
        painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
        painter.fillRect(tinted_pixmap.rect(), text_color)
        painter.end()

        self._close_button.setIcon(QIcon(tinted_pixmap))
        self._close_button.setIconSize(tinted_pixmap.size())
