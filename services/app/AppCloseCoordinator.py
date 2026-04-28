from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Slot
from PySide6.QtWidgets import QMessageBox, QWidget

from services.media.MediaLibraryService import MediaLibraryService
from services.subtitles.facade.SubtitleGenerationService import SubtitleGenerationService
from ui.MessageBoxService import (
    prompt_force_close_background_tasks,
    show_force_close_still_running,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AppCloseResult:
    can_close: bool
    shutdown_completed: bool


class AppClosePhase(Enum):
    IDLE = auto()
    CLOSE_REQUESTED = auto()
    GRACEFUL_SHUTDOWN_STARTED = auto()
    WAITING_FOR_SHUTDOWN = auto()
    WAITING_USER_CHOICE = auto()
    FORCE_SHUTDOWN_STARTED = auto()
    EMERGENCY_SHUTDOWN_STARTED = auto()
    SHUTDOWN_FINISHED = auto()
    FINAL_CLOSE_REQUESTED = auto()


class AppCloseCoordinator(QObject):
    _GRACEFUL_SHUTDOWN_TIMEOUT_MS = 5000
    _FORCE_SHUTDOWN_TIMEOUT_MS = 1500

    def __init__(
        self,
        parent: QWidget,
        subtitle_service: SubtitleGenerationService,
        media_library: MediaLibraryService,
        *,
        shutdown_playback: Callable[[], None],
        is_pip_active: Callable[[], bool],
        teardown_pip_for_shutdown: Callable[[], None],
    ):
        super().__init__(parent)
        self._parent = parent
        self._subtitle_service = subtitle_service
        self._media_library = media_library
        self._shutdown_playback = shutdown_playback
        self._is_pip_active = is_pip_active
        self._teardown_pip_for_shutdown = teardown_pip_for_shutdown
        self._closing_in_progress = False
        self._close_allowed = False
        self._final_close_requested = False
        self._force_requested = False
        self._force_timeout_warning_shown = False
        self._emergency_shutdown_requested = False
        self._timeout_dialog: QMessageBox | None = None
        self._phase = AppClosePhase.IDLE

        self._shutdown_timeout_timer = QTimer(parent)
        self._shutdown_timeout_timer.setSingleShot(True)
        self._shutdown_timeout_timer.timeout.connect(self._on_shutdown_timeout)

        self._subtitle_service.shutdown_finished.connect(self._on_subtitle_shutdown_finished)

    def attempt_close(self) -> AppCloseResult:
        logger.info(
            "Main window close requested | closing_in_progress=%s | close_allowed=%s | phase=%s",
            self._closing_in_progress,
            self._close_allowed,
            self._phase.name,
        )

        if self._close_allowed:
            logger.debug("Main window close accepted because shutdown completion already unlocked closing")
            return AppCloseResult(can_close=True, shutdown_completed=True)

        if self._closing_in_progress:
            logger.info("Repeated main window close request ignored while async shutdown is in progress")
            return AppCloseResult(can_close=False, shutdown_completed=False)

        self._phase = AppClosePhase.CLOSE_REQUESTED

        if self._is_pip_active():
            self._teardown_pip_for_shutdown()

        self._close_allowed = False
        self._final_close_requested = False
        self._force_requested = False
        self._force_timeout_warning_shown = False
        self._emergency_shutdown_requested = False
        self._phase = AppClosePhase.GRACEFUL_SHUTDOWN_STARTED
        has_pending_shutdown = self._subtitle_service.begin_shutdown()
        if self._complete_shutdown_if_synchronous(
            has_pending_shutdown,
            "Application closing immediately because subtitle shutdown completed synchronously",
            request_final_close=False,
        ):
            return AppCloseResult(can_close=True, shutdown_completed=True)

        self._closing_in_progress = True
        self._phase = AppClosePhase.WAITING_FOR_SHUTDOWN
        self._arm_shutdown_timeout(self._GRACEFUL_SHUTDOWN_TIMEOUT_MS)
        logger.info("Application close switched to async shutdown flow")
        return AppCloseResult(can_close=False, shutdown_completed=False)

    def _arm_shutdown_timeout(self, timeout_ms: int):
        self._shutdown_timeout_timer.stop()
        if self._closing_in_progress:
            self._shutdown_timeout_timer.start(timeout_ms)

    def _close_timeout_dialog(self):
        if self._timeout_dialog is None:
            return
        dialog = self._timeout_dialog
        self._timeout_dialog = None
        dialog.close()

    @Slot()
    def _on_shutdown_timeout(self):
        if not self._closing_in_progress or not self._subtitle_service.is_shutdown_in_progress():
            return

        if self._force_requested:
            if self._force_timeout_warning_shown:
                self._request_emergency_shutdown_after_force_timeout()
                return
            logger.warning("Application force shutdown is still waiting for background tasks to terminate")
            self._force_timeout_warning_shown = True
            self._close_timeout_dialog()
            show_force_close_still_running(self._parent)
            self._arm_shutdown_timeout(self._FORCE_SHUTDOWN_TIMEOUT_MS)
            return

        if self._timeout_dialog is not None:
            return

        logger.warning("Application close timeout reached while waiting for subtitle background tasks")
        self._phase = AppClosePhase.WAITING_USER_CHOICE
        self._timeout_dialog = prompt_force_close_background_tasks(
            self._parent,
            on_wait=self._on_wait_after_timeout,
            on_force_close=self._on_force_close_after_timeout,
        )
        self._timeout_dialog.destroyed.connect(self._on_timeout_dialog_destroyed)

    @Slot()
    def _on_wait_after_timeout(self):
        logger.info("User chose to keep waiting for background subtitle tasks during application close")
        self._phase = AppClosePhase.WAITING_FOR_SHUTDOWN
        self._arm_shutdown_timeout(self._GRACEFUL_SHUTDOWN_TIMEOUT_MS)

    @Slot()
    def _on_force_close_after_timeout(self):
        if self._force_requested:
            logger.info("Repeated force-close choice ignored while async force shutdown is already in progress")
            return

        logger.warning("User requested async force close while background subtitle tasks are still stopping")
        self._force_requested = True
        self._force_timeout_warning_shown = False
        self._phase = AppClosePhase.FORCE_SHUTDOWN_STARTED
        has_pending_shutdown = self._subtitle_service.begin_force_shutdown()
        if self._complete_shutdown_if_synchronous(
            has_pending_shutdown,
            "Application force close finished immediately because subtitle shutdown completed synchronously",
            request_final_close=True,
        ):
            return

        self._phase = AppClosePhase.WAITING_FOR_SHUTDOWN
        self._arm_shutdown_timeout(self._FORCE_SHUTDOWN_TIMEOUT_MS)

    def _request_emergency_shutdown_after_force_timeout(self):
        if self._emergency_shutdown_requested:
            logger.critical(
                "Application emergency shutdown timed out; forcing final close without waiting for subtitle shutdown"
            )
            self._finish_shutdown(
                "Application final close forced after emergency shutdown timeout",
                request_final_close=True,
            )
            return

        logger.critical(
            "Application force shutdown timed out again; requesting emergency subtitle shutdown and waiting for completion"
        )
        self._emergency_shutdown_requested = True
        self._phase = AppClosePhase.EMERGENCY_SHUTDOWN_STARTED
        has_pending_shutdown = self._subtitle_service.begin_emergency_shutdown()
        if self._complete_shutdown_if_synchronous(
            has_pending_shutdown,
            "Application emergency shutdown completed synchronously",
            request_final_close=True,
        ):
            return

        self._phase = AppClosePhase.WAITING_FOR_SHUTDOWN
        self._arm_shutdown_timeout(self._FORCE_SHUTDOWN_TIMEOUT_MS)

    @Slot()
    def _on_timeout_dialog_destroyed(self, *_args):
        should_resume_wait = (
            self._closing_in_progress
            and self._phase == AppClosePhase.WAITING_USER_CHOICE
            and not self._close_allowed
            and self._subtitle_service.is_shutdown_in_progress()
        )
        self._timeout_dialog = None
        if should_resume_wait:
            logger.info("Close timeout dialog was dismissed without a choice; resuming shutdown wait")
            self._on_wait_after_timeout()

    @Slot()
    def _on_subtitle_shutdown_finished(self):
        if not self._closing_in_progress:
            return

        self._finish_shutdown(
            "Application shutdown completed",
            request_final_close=True,
        )

    def _complete_shutdown_if_synchronous(
        self,
        has_pending_shutdown: bool,
        completion_log_message: str,
        *,
        request_final_close: bool,
    ) -> bool:
        if has_pending_shutdown or self._subtitle_service.is_shutdown_in_progress():
            return False

        self._finish_shutdown(
            completion_log_message,
            request_final_close=request_final_close,
        )
        return True

    def _finish_shutdown(self, completion_log_message: str, *, request_final_close: bool):
        if self._phase in (AppClosePhase.SHUTDOWN_FINISHED, AppClosePhase.FINAL_CLOSE_REQUESTED):
            logger.debug("Final close request ignored because shutdown completion was already processed")
            return

        logger.info(completion_log_message)
        self._shutdown_timeout_timer.stop()
        self._close_timeout_dialog()
        self._phase = AppClosePhase.SHUTDOWN_FINISHED
        self._complete_local_shutdown()
        if request_final_close:
            self._request_final_close()

    def _complete_local_shutdown(self):
        self._media_library.shutdown()
        self._shutdown_playback()
        self._closing_in_progress = False
        self._force_requested = False
        self._force_timeout_warning_shown = False
        self._emergency_shutdown_requested = False
        self._close_allowed = True

    def _request_final_close(self):
        if self._final_close_requested:
            logger.debug("Final close request ignored because it is already scheduled")
            return

        self._final_close_requested = True
        self._phase = AppClosePhase.FINAL_CLOSE_REQUESTED
        QTimer.singleShot(0, self._parent.close)
