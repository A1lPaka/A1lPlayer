from PySide6.QtCore import QCoreApplication

from services.AppCloseCoordinator import AppCloseCoordinator

from tests.fakes import FakeCloseTarget, FakeMediaStore, FakeSubtitleService


def test_close_without_active_subtitle_tasks_closes_immediately():
    subtitle_service = FakeSubtitleService()
    media_store = FakeMediaStore()
    target = FakeCloseTarget()
    coordinator = AppCloseCoordinator(
        target,
        subtitle_service,
        media_store,
        is_pip_active=lambda: False,
        exit_pip=lambda: None,
    )

    result = coordinator.attempt_close()

    assert result.can_close is True
    assert result.shutdown_completed is True
    assert media_store.shutdown_calls == 1


def test_close_with_active_tasks_uses_async_shutdown_and_repeated_close_is_ignored():
    subtitle_service = FakeSubtitleService()
    subtitle_service.active_tasks = True
    subtitle_service.begin_shutdown_result = True
    media_store = FakeMediaStore()
    target = FakeCloseTarget()
    coordinator = AppCloseCoordinator(
        target,
        subtitle_service,
        media_store,
        is_pip_active=lambda: False,
        exit_pip=lambda: None,
    )

    first = coordinator.attempt_close()
    second = coordinator.attempt_close()

    assert first.can_close is False
    assert second.can_close is False
    assert subtitle_service.begin_shutdown_calls == 1
    assert media_store.shutdown_calls == 0


def test_shutdown_finished_schedules_final_close():
    subtitle_service = FakeSubtitleService()
    subtitle_service.active_tasks = True
    subtitle_service.begin_shutdown_result = True
    media_store = FakeMediaStore()
    target = FakeCloseTarget()
    coordinator = AppCloseCoordinator(
        target,
        subtitle_service,
        media_store,
        is_pip_active=lambda: False,
        exit_pip=lambda: None,
    )

    coordinator.attempt_close()
    subtitle_service.shutdown_in_progress = False
    subtitle_service.shutdown_finished.emit()
    QCoreApplication.processEvents()

    assert media_store.shutdown_calls == 1
    assert target.close_calls == 1


def test_timeout_can_escalate_to_force_close(monkeypatch):
    subtitle_service = FakeSubtitleService()
    subtitle_service.active_tasks = True
    subtitle_service.begin_shutdown_result = True
    subtitle_service.begin_force_shutdown_result = True
    media_store = FakeMediaStore()
    target = FakeCloseTarget()
    force_warning_calls = []
    coordinator = AppCloseCoordinator(
        target,
        subtitle_service,
        media_store,
        is_pip_active=lambda: False,
        exit_pip=lambda: None,
    )

    monkeypatch.setattr(
        "services.AppCloseCoordinator.show_force_close_still_running",
        lambda _parent: force_warning_calls.append(True),
    )

    coordinator.attempt_close()
    coordinator._on_shutdown_timeout()

    assert coordinator._timeout_dialog is not None

    coordinator._on_force_close_after_timeout()

    assert subtitle_service.begin_force_shutdown_calls == 1

    coordinator._on_shutdown_timeout()

    assert force_warning_calls == [True]

