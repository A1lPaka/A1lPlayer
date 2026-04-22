from PySide6.QtCore import QCoreApplication

from services.AppCloseCoordinator import AppCloseCoordinator

from tests.fakes import FakeCloseTarget, FakeMediaLibrary, FakePlaybackShutdown, FakeSubtitleService


def test_close_without_active_subtitle_tasks_closes_immediately():
    subtitle_service = FakeSubtitleService()
    media_library = FakeMediaLibrary()
    playback = FakePlaybackShutdown()
    target = FakeCloseTarget()
    coordinator = AppCloseCoordinator(
        target,
        subtitle_service,
        media_library,
        shutdown_playback=playback.shutdown,
        is_pip_active=lambda: False,
        teardown_pip_for_shutdown=lambda: None,
    )

    result = coordinator.attempt_close()

    assert result.can_close is True
    assert result.shutdown_completed is True
    assert subtitle_service.begin_shutdown_calls == 1
    assert media_library.shutdown_calls == 1
    assert playback.shutdown_calls == 1


def test_close_with_active_tasks_uses_async_shutdown_and_repeated_close_is_ignored():
    subtitle_service = FakeSubtitleService()
    subtitle_service.begin_shutdown_result = True
    media_library = FakeMediaLibrary()
    playback = FakePlaybackShutdown()
    target = FakeCloseTarget()
    coordinator = AppCloseCoordinator(
        target,
        subtitle_service,
        media_library,
        shutdown_playback=playback.shutdown,
        is_pip_active=lambda: False,
        teardown_pip_for_shutdown=lambda: None,
    )

    first = coordinator.attempt_close()
    second = coordinator.attempt_close()

    assert first.can_close is False
    assert second.can_close is False
    assert subtitle_service.begin_shutdown_calls == 1
    assert media_library.shutdown_calls == 0
    assert playback.shutdown_calls == 0


def test_shutdown_finished_schedules_final_close():
    subtitle_service = FakeSubtitleService()
    subtitle_service.begin_shutdown_result = True
    media_library = FakeMediaLibrary()
    playback = FakePlaybackShutdown()
    target = FakeCloseTarget()
    coordinator = AppCloseCoordinator(
        target,
        subtitle_service,
        media_library,
        shutdown_playback=playback.shutdown,
        is_pip_active=lambda: False,
        teardown_pip_for_shutdown=lambda: None,
    )

    coordinator.attempt_close()
    subtitle_service.shutdown_in_progress = False
    subtitle_service.shutdown_finished.emit()
    QCoreApplication.processEvents()

    assert media_library.shutdown_calls == 1
    assert playback.shutdown_calls == 1
    assert target.close_calls == 1


def test_timeout_can_escalate_to_force_close(monkeypatch):
    subtitle_service = FakeSubtitleService()
    subtitle_service.begin_shutdown_result = True
    subtitle_service.begin_force_shutdown_result = True
    media_library = FakeMediaLibrary()
    playback = FakePlaybackShutdown()
    target = FakeCloseTarget()
    force_warning_calls = []
    coordinator = AppCloseCoordinator(
        target,
        subtitle_service,
        media_library,
        shutdown_playback=playback.shutdown,
        is_pip_active=lambda: False,
        teardown_pip_for_shutdown=lambda: None,
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
    assert playback.shutdown_calls == 0


def test_repeated_force_timeout_performs_emergency_close(monkeypatch):
    subtitle_service = FakeSubtitleService()
    subtitle_service.begin_shutdown_result = True
    subtitle_service.begin_force_shutdown_result = True
    media_library = FakeMediaLibrary()
    playback = FakePlaybackShutdown()
    target = FakeCloseTarget()
    force_warning_calls = []
    coordinator = AppCloseCoordinator(
        target,
        subtitle_service,
        media_library,
        shutdown_playback=playback.shutdown,
        is_pip_active=lambda: False,
        teardown_pip_for_shutdown=lambda: None,
    )

    monkeypatch.setattr(
        "services.AppCloseCoordinator.show_force_close_still_running",
        lambda _parent: force_warning_calls.append(True),
    )

    coordinator.attempt_close()
    coordinator._on_shutdown_timeout()
    coordinator._on_force_close_after_timeout()
    coordinator._on_shutdown_timeout()
    coordinator._on_shutdown_timeout()
    QCoreApplication.processEvents()

    assert force_warning_calls == [True]
    assert subtitle_service.begin_force_shutdown_calls == 1
    assert media_library.shutdown_calls == 1
    assert playback.shutdown_calls == 1
    assert target.close_calls == 1


def test_repeated_force_close_choice_does_not_repeat_force_shutdown():
    subtitle_service = FakeSubtitleService()
    subtitle_service.begin_shutdown_result = True
    subtitle_service.begin_force_shutdown_result = True
    media_library = FakeMediaLibrary()
    playback = FakePlaybackShutdown()
    target = FakeCloseTarget()
    coordinator = AppCloseCoordinator(
        target,
        subtitle_service,
        media_library,
        shutdown_playback=playback.shutdown,
        is_pip_active=lambda: False,
        teardown_pip_for_shutdown=lambda: None,
    )

    coordinator.attempt_close()
    coordinator._on_shutdown_timeout()
    coordinator._on_force_close_after_timeout()
    coordinator._on_force_close_after_timeout()

    assert subtitle_service.begin_force_shutdown_calls == 1
    assert media_library.shutdown_calls == 0
    assert playback.shutdown_calls == 0


def test_force_close_with_synchronous_shutdown_requests_final_close():
    subtitle_service = FakeSubtitleService()
    subtitle_service.begin_shutdown_result = True
    subtitle_service.begin_force_shutdown_result = False
    media_library = FakeMediaLibrary()
    playback = FakePlaybackShutdown()
    target = FakeCloseTarget()
    coordinator = AppCloseCoordinator(
        target,
        subtitle_service,
        media_library,
        shutdown_playback=playback.shutdown,
        is_pip_active=lambda: False,
        teardown_pip_for_shutdown=lambda: None,
    )

    coordinator.attempt_close()
    coordinator._on_shutdown_timeout()
    coordinator._on_force_close_after_timeout()
    QCoreApplication.processEvents()

    assert subtitle_service.begin_force_shutdown_calls == 1
    assert media_library.shutdown_calls == 1
    assert playback.shutdown_calls == 1
    assert target.close_calls == 1


def test_close_with_active_pip_uses_shutdown_specific_pip_teardown():
    subtitle_service = FakeSubtitleService()
    media_library = FakeMediaLibrary()
    playback = FakePlaybackShutdown()
    target = FakeCloseTarget()
    teardown_calls = []
    coordinator = AppCloseCoordinator(
        target,
        subtitle_service,
        media_library,
        shutdown_playback=playback.shutdown,
        is_pip_active=lambda: True,
        teardown_pip_for_shutdown=lambda: teardown_calls.append(True),
    )

    result = coordinator.attempt_close()

    assert result.can_close is True
    assert result.shutdown_completed is True
    assert teardown_calls == [True]
