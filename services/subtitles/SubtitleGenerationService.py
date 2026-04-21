import logging
import os
import time
from dataclasses import replace
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QCoreApplication, QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import QWidget

from models import SubtitleGenerationDialogResult
from services.MediaSettingsStore import MediaSettingsStore
from services.MediaLibraryService import SubtitleAttachResult
from services.runtime.WorkerStopControl import call_worker_stop
from services.subtitles.CudaRuntimeDiscovery import get_missing_windows_cuda_runtime_packages
from services.subtitles.SubtitleCudaRuntimeFlow import SubtitleCudaRuntimeFlow
from services.subtitles.SubtitleGenerationAudioProbeFlow import SubtitleGenerationAudioProbeFlow
from services.subtitles.SubtitleGenerationPreflight import (
    SubtitleGenerationPreflight,
    SubtitleGenerationValidationFailure,
    SubtitleGenerationValidationResult,
)
from services.subtitles.SubtitlePipelineState import (
    SubtitleGenerationContext,
    SubtitlePipelinePhase,
    SubtitlePipelineRun,
    SubtitlePipelineStateMachine,
    SubtitlePipelineTask,
    SubtitleServiceState,
)
from services.subtitles.SubtitleGenerationUiCoordinator import SubtitleGenerationUiCoordinator
from services.subtitles.SubtitleGenerationValidationPresenter import SubtitleGenerationValidationPresenter
from services.subtitles.SubtitleGenerationWorkers import SubtitleGenerationWorker
from services.subtitles.SubtitleTiming import elapsed_ms_since, log_timing
from ui.MessageBoxService import (
    prompt_cuda_runtime_choice,
    show_cuda_runtime_install_canceled,
    show_cuda_runtime_install_failed,
    show_subtitle_auto_load_failed,
    show_subtitle_created,
    show_subtitle_created_with_fallback_name,
    show_subtitle_created_not_loaded_due_to_context_change,
    show_subtitle_generation_already_running,
    show_subtitle_generation_canceled,
    show_subtitle_generation_failed,
)
from ui.PlayerWindow import PlayerWindow


logger = logging.getLogger(__name__)


class SubtitleAutoOpenOutcome(Enum):
    LOADED = auto()
    CONTEXT_CHANGED = auto()
    LOAD_FAILED = auto()


if TYPE_CHECKING:
    from services.MediaLibraryService import MediaLibraryService


class SubtitleGenerationService(QObject):
    shutdown_finished = Signal()
    _PLAYBACK_INTERRUPTION_OWNER = "subtitle_generation"

    def __init__(
        self,
        parent: QWidget,
        player_window: PlayerWindow,
        store: MediaSettingsStore,
        media_library: "MediaLibraryService",
    ):
        super().__init__(parent)
        self._parent = parent
        self._player = player_window
        self._store = store
        self._media_library = media_library
        self._ui = SubtitleGenerationUiCoordinator(
            parent,
            theme_color_getter=lambda: self._player.theme_color,
        )
        self._preflight = SubtitleGenerationPreflight(parent)
        self._validation_presenter = SubtitleGenerationValidationPresenter(parent)
        self._pipeline_state = SubtitlePipelineStateMachine()
        self._pending_subtitle_thread_run_ids: set[int] = set()
        self._shutdown_completed = False
        self._force_shutdown_requested = False
        self._playback_takeover = self._player.playback.create_interruption_lease(
            self._PLAYBACK_INTERRUPTION_OWNER,
        )
        self._player_ui_suspend_lease = None
        self._dialog_request_started_at: float | None = None
        self._dialog_request_media_path: str | None = None
        self._audio_probe_flow = SubtitleGenerationAudioProbeFlow(
            parent,
            self._player,
            self._ui,
            self._preflight,
            is_generation_dialog_open=lambda: self._pipeline_state.service_state == SubtitleServiceState.DIALOG_OPEN,
            dialog_media_path=lambda: self._dialog_request_media_path,
            service_state_name=lambda: self._pipeline_state.service_state.name,
        )
        self._audio_probe_flow.thread_finished.connect(self._complete_shutdown_if_possible)
        self._cuda_runtime_flow = SubtitleCudaRuntimeFlow(parent)
        self._cuda_runtime_flow.status_changed.connect(self._on_worker_status_changed)
        self._cuda_runtime_flow.details_changed.connect(self._on_worker_details_changed)
        self._cuda_runtime_flow.finished.connect(self._on_cuda_runtime_install_finished)
        self._cuda_runtime_flow.failed.connect(self._on_cuda_runtime_install_failed)
        self._cuda_runtime_flow.canceled.connect(self._on_cuda_runtime_install_canceled)
        self._cuda_runtime_flow.thread_finished.connect(self._on_cuda_runtime_flow_thread_finished)

    def generate_subtitle(self) -> bool:
        self._assert_pipeline_thread()
        current_media_path = self._player.playback.current_media_path()
        if not current_media_path:
            logger.info("Subtitle generation requested without an active media item")
            return False

        if self._shutdown_completed or self._pipeline_state.service_state == SubtitleServiceState.SHUTTING_DOWN:
            logger.info("Subtitle generation request ignored because shutdown is in progress")
            return False

        if self._pipeline_state.active_run is not None and self._pipeline_state.active_run.blocks_new_requests():
            self._ui.focus_active_dialog()
            show_subtitle_generation_already_running(self._parent)
            logger.info("Subtitle generation request ignored because another background task is running")
            return False

        if self._pipeline_state.service_state == SubtitleServiceState.DIALOG_OPEN:
            self._ui.focus_active_dialog()
            logger.info("Subtitle generation request focused the existing generation dialog")
            return False

        if not self._pipeline_state.transition_service_state(
            SubtitleServiceState.DIALOG_OPEN,
            "open generation dialog",
            allowed=(SubtitleServiceState.IDLE,),
        ):
            return False

        self._playback_takeover.acquire()
        self._dialog_request_started_at = time.perf_counter()
        self._dialog_request_media_path = current_media_path
        self._ui.open_generation_dialog(
            current_media_path,
            on_generate=self._start_subtitle_generation,
            on_cancel=self._on_generation_dialog_canceled,
        )
        self._audio_probe_flow.load_generation_audio_tracks_async(current_media_path)
        return True

    def _start_subtitle_generation(self, options: SubtitleGenerationDialogResult):
        self._assert_pipeline_thread()
        self._log_dialog_confirm_timing(options.output_path)
        if self._pipeline_state.service_state != SubtitleServiceState.DIALOG_OPEN:
            logger.warning(
                "Rejected subtitle generation start because the generation dialog is not active | state=%s",
                self._pipeline_state.service_state.name,
            )
            return

        current_media_path = self._player.playback.current_media_path()
        if not current_media_path:
            logger.warning("Subtitle generation aborted because current media path disappeared before launch")
            self._transition_back_to_dialog("media path disappeared before launch")
            return

        generation_context = self._capture_current_generation_context()
        if generation_context is None:
            logger.warning("Subtitle generation aborted because playback context is unavailable before launch")
            self._transition_back_to_dialog("playback context unavailable before launch")
            return

        run = self._pipeline_state.begin_run(generation_context, options)
        preflight_started_at = time.perf_counter()
        validation_result = self._preflight.validate_generation_request(
            current_media_path,
            options,
            probe_state=self._audio_probe_flow.probe_state_for_media(current_media_path),
            audio_streams=self._audio_probe_flow.get_cached_audio_streams_for_media(current_media_path),
            probe_error=self._audio_probe_flow.get_cached_audio_stream_error_for_media(current_media_path),
        )
        log_timing(
            logger,
            "Subtitle timing",
            "preflight_validation",
            elapsed_ms_since(preflight_started_at),
            run_id=run.run_id,
            media=run.context.media_path,
            output=options.output_path,
        )
        if not self._validation_presenter.confirm_or_show_failure(validation_result):
            self._discard_starting_run("subtitle generation preflight failed")
            return
        options = self._apply_overwrite_confirmation(options, validation_result)
        run.requested_options = options

        resolved_options = self._resolve_cuda_runtime_options(options, run)
        if resolved_options is None:
            if self._pipeline_state.active_run is run and run.phase == SubtitlePipelinePhase.STARTING:
                self._discard_starting_run("subtitle launch postponed or canceled during CUDA resolution")
            return

        latest_context = self._capture_current_generation_context()
        if latest_context is None:
            logger.warning("Subtitle generation aborted because playback context is unavailable before launch")
            self._pipeline_state.discard_active_run()
            self._transition_back_to_dialog("playback context unavailable before launch")
            return
        if latest_context != run.context:
            logger.warning(
                "Subtitle generation aborted because playback context changed before launch | run_id=%s | original_media=%s | original_request_id=%s | active_media=%s | active_request_id=%s",
                run.run_id,
                run.context.media_path,
                run.context.request_id,
                latest_context.media_path,
                latest_context.request_id,
            )
            self._pipeline_state.discard_active_run()
            self._transition_back_to_dialog("playback context changed before launch")
            return

        run.subtitle_options = resolved_options
        self._launch_subtitle_generation(run, resolved_options)

    def _launch_subtitle_generation(
        self,
        run: SubtitlePipelineRun,
        options: SubtitleGenerationDialogResult,
    ):
        self._assert_pipeline_thread()
        if run is not self._pipeline_state.active_run:
            logger.debug("Ignoring subtitle worker launch for stale run | run_id=%s", run.run_id)
            return
        if run.phase not in (SubtitlePipelinePhase.STARTING, SubtitlePipelinePhase.RUNNING):
            logger.warning(
                "Rejected subtitle worker launch because run phase is not launchable | run_id=%s | phase=%s",
                run.run_id,
                run.phase.name,
            )
            return

        self._pipeline_state.set_run_phase(run, SubtitlePipelinePhase.RUNNING, "launch subtitle generation worker")
        if self._pipeline_state.service_state != SubtitleServiceState.SHUTTING_DOWN:
            self._pipeline_state.transition_service_state(
                SubtitleServiceState.IDLE,
                "generation dialog replaced by subtitle progress",
                allowed=(SubtitleServiceState.DIALOG_OPEN, SubtitleServiceState.IDLE),
            )

        logger.info(
            "Launching subtitle generation | run_id=%s | media=%s | request_id=%s | output=%s | format=%s | requested_device=%s | model=%s",
            run.run_id,
            run.context.media_path,
            run.context.request_id,
            options.output_path,
            options.output_format,
            options.device or "auto",
            options.model_size,
        )
        run.task = SubtitlePipelineTask.SUBTITLE_GENERATION
        self._ui.open_generation_progress(options, on_cancel=self._request_active_task_stop)

        launch_preparation_started_at = time.perf_counter()
        thread = QThread(self._parent)
        worker = SubtitleGenerationWorker(run.run_id, run.context.media_path, options)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.status_changed.connect(self._on_worker_status_changed_from_worker, Qt.QueuedConnection)
        worker.progress_changed.connect(self._on_worker_progress_changed_from_worker, Qt.QueuedConnection)
        worker.details_changed.connect(self._on_worker_details_changed_from_worker, Qt.QueuedConnection)
        worker.finished.connect(self._on_subtitle_generation_finished_from_worker, Qt.QueuedConnection)
        worker.failed.connect(self._on_subtitle_generation_failed_from_worker, Qt.QueuedConnection)
        worker.canceled.connect(self._on_subtitle_generation_canceled_from_worker, Qt.QueuedConnection)

        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.canceled.connect(thread.quit)
        thread.finished.connect(
            lambda run_id=run.run_id: self._on_background_task_thread_finished(
                run_id,
                SubtitlePipelineTask.SUBTITLE_GENERATION,
            )
        )
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        run.subtitle_thread = thread
        run.subtitle_worker = worker
        run.subtitle_cancel_requested = False
        self._pending_subtitle_thread_run_ids.add(run.run_id)
        QTimer.singleShot(
            0,
            lambda run_id=run.run_id, thread=thread: self._deferred_suspend_and_start_subtitle_worker(run_id, thread),
        )
        log_timing(
            logger,
            "Subtitle timing",
            "worker_launch_preparation",
            elapsed_ms_since(launch_preparation_started_at),
            run_id=run.run_id,
            media=run.context.media_path,
            output=options.output_path,
        )

    def _resolve_cuda_runtime_options(
        self,
        options: SubtitleGenerationDialogResult,
        run: SubtitlePipelineRun,
    ) -> SubtitleGenerationDialogResult | None:
        self._assert_pipeline_thread()
        if options.device != "cuda":
            return options

        missing_packages = get_missing_windows_cuda_runtime_packages()
        if not missing_packages:
            return options

        logger.info(
            "CUDA runtime missing for subtitle generation | run_id=%s | media=%s | request_id=%s | packages=%s",
            run.run_id,
            run.context.media_path,
            run.context.request_id,
            ", ".join(missing_packages),
        )
        choice = prompt_cuda_runtime_choice(self._parent, missing_packages)
        if not self._starting_run_matches_current_context(run, "CUDA runtime prompt"):
            return None

        if choice == "cancel":
            logger.info("User canceled subtitle generation after CUDA runtime prompt | run_id=%s", run.run_id)
            return None

        if choice == "cpu":
            logger.info("User switched subtitle generation from CUDA to CPU | run_id=%s", run.run_id)
            return replace(options, device="cpu")

        run.subtitle_options = options
        self._start_cuda_runtime_install(run, missing_packages)
        return None

    def _starting_run_matches_current_context(self, run: SubtitlePipelineRun, event_name: str) -> bool:
        self._assert_pipeline_thread()
        if run is not self._pipeline_state.active_run:
            logger.debug("Ignoring %s result for stale subtitle pipeline run | run_id=%s", event_name, run.run_id)
            return False
        if run.phase != SubtitlePipelinePhase.STARTING:
            logger.warning(
                "Rejected %s result because run phase is not launchable | run_id=%s | phase=%s",
                event_name,
                run.run_id,
                run.phase.name,
            )
            return False
        latest_context = self._capture_current_generation_context()
        if latest_context == run.context:
            return True

        logger.warning(
            "Rejected %s result because playback context changed | run_id=%s | original_media=%s | original_request_id=%s | active_media=%s | active_request_id=%s",
            event_name,
            run.run_id,
            run.context.media_path,
            run.context.request_id,
            latest_context.media_path if latest_context is not None else "<none>",
            latest_context.request_id if latest_context is not None else "<none>",
        )
        return False

    def _start_cuda_runtime_install(
        self,
        run: SubtitlePipelineRun,
        missing_packages: list[str],
    ):
        self._assert_pipeline_thread()
        if run is not self._pipeline_state.active_run:
            logger.debug("Ignoring CUDA runtime install start for stale run | run_id=%s", run.run_id)
            return
        if run.phase != SubtitlePipelinePhase.STARTING:
            logger.warning(
                "Rejected CUDA runtime install start because run phase is not launchable | run_id=%s | phase=%s",
                run.run_id,
                run.phase.name,
            )
            return

        self._pipeline_state.set_run_phase(run, SubtitlePipelinePhase.RUNNING, "start CUDA runtime install")
        if self._pipeline_state.service_state != SubtitleServiceState.SHUTTING_DOWN:
            self._pipeline_state.transition_service_state(
                SubtitleServiceState.IDLE,
                "generation dialog replaced by CUDA runtime progress",
                allowed=(SubtitleServiceState.DIALOG_OPEN, SubtitleServiceState.IDLE),
            )

        logger.info(
            "Starting CUDA runtime install flow | run_id=%s | media=%s | request_id=%s | packages=%s",
            run.run_id,
            run.context.media_path,
            run.context.request_id,
            ", ".join(missing_packages),
        )
        run.task = SubtitlePipelineTask.CUDA_INSTALL
        self._ui.open_cuda_install_progress(
            missing_packages,
            on_cancel=self._request_active_task_stop,
        )
        if not self._cuda_runtime_flow.start(run.run_id, missing_packages):
            logger.error("CUDA runtime install flow could not be started | run_id=%s", run.run_id)
            self._complete_run(
                run.run_id,
                SubtitlePipelinePhase.FAILED,
                close_progress=True,
            )
            if self._pipeline_state.service_state != SubtitleServiceState.SHUTTING_DOWN:
                show_cuda_runtime_install_failed(self._parent, "GPU runtime installation could not be started.")
            return

    def _deferred_suspend_and_start_subtitle_worker(self, run_id: int, thread: QThread):
        run = self._require_active_run(run_id, "deferred subtitle worker launch")
        if run is None:
            return

        if run.subtitle_thread is not thread or run.subtitle_worker is None:
            logger.debug("Skipping deferred subtitle worker launch because worker references changed | run_id=%s", run_id)
            return

        if run.phase not in (
            SubtitlePipelinePhase.RUNNING,
            SubtitlePipelinePhase.CANCELING,
        ):
            logger.debug(
                "Skipping deferred subtitle worker launch because run phase changed | run_id=%s | phase=%s",
                run_id,
                run.phase.name,
            )
            return

        self._suspend_player_ui_for_generation()
        QTimer.singleShot(
            0,
            lambda run_id=run_id, thread=thread: self._deferred_start_subtitle_worker(run_id, thread),
        )

    def _deferred_start_subtitle_worker(self, run_id: int, thread: QThread):
        run = self._require_active_run(run_id, "deferred subtitle worker thread start")
        if run is None:
            return

        if run.subtitle_thread is not thread or run.subtitle_worker is None:
            logger.debug("Skipping deferred subtitle worker thread start because worker references changed | run_id=%s", run_id)
            return

        if thread.isRunning():
            logger.debug("Skipping deferred subtitle worker thread start because thread is already running | run_id=%s", run_id)
            return

        thread.start()

    @Slot()
    def _request_active_task_stop(self, force: bool = False):
        self._assert_pipeline_thread()
        run = self._pipeline_state.active_run
        if run is None or not run.accepts_stop_requests():
            logger.debug(
                "Active task stop ignored because pipeline is not stoppable | run_phase=%s | force=%s",
                run.phase.name if run is not None else "<none>",
                force,
            )
            return

        if run.task == SubtitlePipelineTask.NONE:
            logger.debug("Active task stop ignored because no pipeline task is active | force=%s", force)
            return

        self._pipeline_state.set_run_phase(run, SubtitlePipelinePhase.CANCELING, f"request stop for {run.task.name.lower()}")

        run_id = self._current_run_id()
        if run.task == SubtitlePipelineTask.SUBTITLE_GENERATION:
            if run.subtitle_worker is None:
                logger.debug("Subtitle generation stop ignored because no worker is active | force=%s", force)
                return

            if force:
                logger.warning("Force-stop requested for subtitle generation worker | run_id=%s", run_id)
                call_worker_stop(run.subtitle_worker, "force_stop")
                return

            if run.subtitle_cancel_requested:
                logger.info("Repeated stop request ignored for subtitle generation worker")
                return

            run.subtitle_cancel_requested = True
            logger.info("Cancel requested for subtitle generation worker | run_id=%s", run_id)
            call_worker_stop(run.subtitle_worker, "cancel")
            self._ui.show_subtitle_cancel_pending()
            return

        requested = self._cuda_runtime_flow.request_stop(force=force)
        if not requested:
            return

        if force:
            logger.warning("Force-stop requested for CUDA runtime install flow | run_id=%s", run_id)
            return

        logger.info("Cancel requested for CUDA runtime install flow | run_id=%s", run_id)
        self._ui.show_cuda_install_cancel_pending()

    def has_active_tasks(self) -> bool:
        if (
            self._pending_subtitle_thread_run_ids
            or self._cuda_runtime_flow.is_active()
            or self._audio_probe_flow.is_active()
        ):
            return True

        run = self._pipeline_state.active_run
        if run is None:
            return False

        return run.keeps_shutdown_pending()

    def is_shutdown_in_progress(self) -> bool:
        return self._pipeline_state.service_state == SubtitleServiceState.SHUTTING_DOWN and not self._shutdown_completed

    def begin_shutdown(self) -> bool:
        self._assert_pipeline_thread()
        if self._shutdown_completed:
            logger.debug("Subtitle generation service shutdown requested after completion")
            return False

        if self._pipeline_state.service_state == SubtitleServiceState.SHUTTING_DOWN:
            logger.info(
                "Subtitle generation service shutdown already in progress | force_requested=%s",
                self._force_shutdown_requested,
            )
            return self.has_active_tasks()

        logger.info("Subtitle generation service async shutdown started | state=%s", self._pipeline_state.service_state.name)
        self._pipeline_state.transition_service_state(
            SubtitleServiceState.SHUTTING_DOWN,
            "begin graceful shutdown",
            allowed=tuple(SubtitleServiceState),
        )
        self._force_shutdown_requested = False
        self._ui.close_generation_dialog()
        self._request_active_task_stop(force=False)
        self._audio_probe_flow.invalidate_active_request("shutdown")
        self._complete_shutdown_if_possible()
        return self.has_active_tasks()

    def begin_force_shutdown(self) -> bool:
        self._assert_pipeline_thread()
        if self._shutdown_completed:
            logger.debug("Subtitle generation service force shutdown requested after completion")
            return False

        if self._pipeline_state.service_state != SubtitleServiceState.SHUTTING_DOWN:
            logger.warning("Subtitle generation service force shutdown requested before graceful shutdown")
            self._pipeline_state.transition_service_state(
                SubtitleServiceState.SHUTTING_DOWN,
                "begin force shutdown",
                allowed=tuple(SubtitleServiceState),
            )
            self._ui.close_generation_dialog()

        if self._force_shutdown_requested:
            logger.info("Repeated force shutdown request ignored for subtitle generation service")
            return self.has_active_tasks()

        logger.warning("Subtitle generation service async force shutdown started")
        self._force_shutdown_requested = True
        self._ui.close_progress_dialog()
        self._request_active_task_stop(force=True)
        self._audio_probe_flow.stop_all("shutdown", force=True)
        self._complete_shutdown_if_possible()
        return self.has_active_tasks()

    def _finalize_shutdown_service_state(self):
        self._assert_pipeline_thread()
        self._ui.close_progress_dialog()
        self._pipeline_state.discard_active_run()
        self._audio_probe_flow.stop_all("finalize-shutdown", force=True)
        self._release_playback_takeover(resume_playback=False)
        self._shutdown_completed = True
        self._force_shutdown_requested = False

    def _complete_shutdown_if_possible(self):
        if self._pipeline_state.service_state != SubtitleServiceState.SHUTTING_DOWN or self._shutdown_completed:
            return

        if self.has_active_tasks():
            active_task = self._pipeline_state.active_run.task.name if self._pipeline_state.active_run is not None else "NONE"
            active_phase = self._pipeline_state.active_run.phase.name if self._pipeline_state.active_run is not None else "<none>"
            logger.debug(
                "Subtitle generation service shutdown still waiting for background tasks | task=%s | phase=%s | force_requested=%s",
                active_task,
                active_phase,
                self._force_shutdown_requested,
            )
            return

        self._finalize_shutdown_service_state()
        logger.info("Subtitle generation service shutdown finished")
        self.shutdown_finished.emit()

    @Slot()
    def _on_generation_dialog_canceled(self):
        self._assert_pipeline_thread()
        if self._pipeline_state.service_state != SubtitleServiceState.DIALOG_OPEN:
            return

        self._audio_probe_flow.invalidate_active_request("dialog closed")
        self._clear_dialog_request_timing()
        logger.info("Subtitle generation dialog closed without launching a job")
        self._pipeline_state.transition_service_state(
            SubtitleServiceState.IDLE,
            "close generation dialog",
            allowed=(SubtitleServiceState.DIALOG_OPEN,),
        )
        self._release_playback_takeover(resume_playback=True)

    def _on_background_task_thread_finished(self, run_id: int, task: SubtitlePipelineTask):
        self._assert_pipeline_thread()
        if task == SubtitlePipelineTask.SUBTITLE_GENERATION:
            self._pending_subtitle_thread_run_ids.discard(run_id)

        run = self._pipeline_state.active_run if self._pipeline_state.active_run is not None and self._pipeline_state.active_run.run_id == run_id else None
        if run is not None and task == SubtitlePipelineTask.SUBTITLE_GENERATION:
            self._clear_subtitle_runtime(run)

        if run is not None:
            logger.debug(
                "Background task thread finished for active subtitle pipeline run | run_id=%s | task=%s | phase=%s",
                run_id,
                task.name,
                run.phase.name,
            )
        else:
            logger.debug(
                "Background task thread finished for stale subtitle pipeline run | run_id=%s | task=%s | state=%s",
                run_id,
                task.name,
                self._pipeline_state.service_state.name,
            )

        if run is not None and self._run_is_terminal(run):
            self._pipeline_state.discard_active_run()
        self._complete_shutdown_if_possible()

    def _on_cuda_runtime_flow_thread_finished(self, run_id: int):
        self._on_background_task_thread_finished(run_id, SubtitlePipelineTask.CUDA_INSTALL)

    def _current_run_id_for_active_subtitle_worker(self, event_name: str) -> int | None:
        run = self._pipeline_state.active_run
        if run is None or run.subtitle_worker is None:
            logger.debug("Ignoring %s because no subtitle worker is active", event_name)
            return None

        sender = self.sender()
        if sender is not run.subtitle_worker:
            logger.debug(
                "Ignoring %s from stale subtitle worker | sender_matches_active=%s",
                event_name,
                sender is run.subtitle_worker,
            )
            return None

        return run.run_id

    def _forward_active_subtitle_worker_event(self, event_name: str, handler, *args):
        run_id = self._current_run_id_for_active_subtitle_worker(event_name)
        if run_id is None:
            return
        handler(run_id, *args)

    @Slot(str)
    def _on_worker_status_changed_from_worker(self, text: str):
        self._forward_active_subtitle_worker_event("status update", self._on_worker_status_changed, text)

    @Slot(int)
    def _on_worker_progress_changed_from_worker(self, value: int):
        self._forward_active_subtitle_worker_event("progress update", self._on_worker_progress_changed, value)

    @Slot(str)
    def _on_worker_details_changed_from_worker(self, text: str):
        self._forward_active_subtitle_worker_event("details update", self._on_worker_details_changed, text)

    @Slot(str, bool, bool)
    def _on_subtitle_generation_finished_from_worker(self, output_path: str, auto_open: bool, used_fallback_output_path: bool):
        self._forward_active_subtitle_worker_event(
            "subtitle generation finished",
            self._on_subtitle_generation_finished,
            output_path,
            auto_open,
            used_fallback_output_path,
        )

    @Slot(str, str)
    def _on_subtitle_generation_failed_from_worker(self, error_text: str, diagnostics: str):
        self._forward_active_subtitle_worker_event(
            "subtitle generation failed",
            self._on_subtitle_generation_failed,
            error_text,
            diagnostics,
        )

    @Slot()
    def _on_subtitle_generation_canceled_from_worker(self):
        self._forward_active_subtitle_worker_event(
            "subtitle generation canceled",
            self._on_subtitle_generation_canceled,
        )

    def _on_worker_status_changed(self, run_id: int, text: str):
        if not self._is_current_run_event(run_id, "status update"):
            return
        self._ui.update_progress_status(text)

    def _on_worker_progress_changed(self, run_id: int, value: int):
        if not self._is_current_run_event(run_id, "progress update"):
            return
        self._ui.update_progress(value)

    def _on_worker_details_changed(self, run_id: int, text: str):
        if not self._is_current_run_event(run_id, "details update"):
            return
        self._ui.update_progress_details(text)

    def _on_subtitle_generation_finished(self, run_id: int, output_path: str, auto_open: bool, used_fallback_output_path: bool):
        run = self._require_active_run(run_id, "subtitle generation finished")
        if run is None:
            return

        logger.info(
            "Subtitle generation finished | run_id=%s | media=%s | request_id=%s | output=%s | auto_open=%s",
            run.run_id,
            run.context.media_path,
            run.context.request_id,
            output_path,
            auto_open,
        )

        self._complete_run(
            run_id,
            SubtitlePipelinePhase.SUCCEEDED,
            close_progress=True,
        )

        if self._pipeline_state.service_state == SubtitleServiceState.SHUTTING_DOWN:
            return

        auto_open_outcome = SubtitleAutoOpenOutcome.LOADED
        if auto_open:
            attach_result = self._media_library.attach_subtitle(
                output_path,
                source="generated",
                save_last_dir=True,
                guard_media_path=run.context.media_path,
                guard_request_id=run.context.request_id,
            )
            if attach_result == SubtitleAttachResult.CONTEXT_CHANGED:
                auto_open_outcome = SubtitleAutoOpenOutcome.CONTEXT_CHANGED
            elif attach_result == SubtitleAttachResult.LOAD_FAILED:
                logger.error("Generated subtitle could not be auto-loaded into playback | run_id=%s | output=%s", run.run_id, output_path)
                auto_open_outcome = SubtitleAutoOpenOutcome.LOAD_FAILED
        else:
            self._store.save_last_open_dir(output_path)

        self._show_generation_success_outcome(
            output_path,
            auto_open_outcome,
            used_fallback_output_path=used_fallback_output_path,
            requested_output_path=(run.subtitle_options or run.requested_options).output_path,
        )

    def _on_subtitle_generation_failed(self, run_id: int, error_text: str, diagnostics: str):
        run = self._require_active_run(run_id, "subtitle generation failed")
        if run is None:
            return

        if diagnostics:
            logger.error(
                "Subtitle generation failed | run_id=%s | message=%s | diagnostics=%s",
                run.run_id,
                error_text,
                diagnostics,
            )
        else:
            logger.error("Subtitle generation failed | run_id=%s | message=%s", run.run_id, error_text)

        self._complete_run(
            run_id,
            SubtitlePipelinePhase.FAILED,
            close_progress=True,
        )

        if self._pipeline_state.service_state == SubtitleServiceState.SHUTTING_DOWN:
            return

        show_subtitle_generation_failed(self._parent, error_text)

    def _on_subtitle_generation_canceled(self, run_id: int):
        run = self._require_active_run(run_id, "subtitle generation canceled")
        if run is None:
            return

        logger.info("Subtitle generation canceled | run_id=%s", run.run_id)
        self._complete_run(
            run_id,
            SubtitlePipelinePhase.CANCELED,
            close_progress=True,
        )

        if self._pipeline_state.service_state == SubtitleServiceState.SHUTTING_DOWN:
            return

        show_subtitle_generation_canceled(self._parent)

    def _on_cuda_runtime_install_finished(self, run_id: int):
        run = self._require_active_run(run_id, "CUDA runtime install finished")
        if run is None:
            return

        logger.info("CUDA runtime install flow finished | run_id=%s", run.run_id)
        self._ui.close_progress_dialog()

        if run.phase == SubtitlePipelinePhase.CANCELING:
            logger.info("Ignoring CUDA runtime completion because pipeline cancellation is already in progress | run_id=%s", run.run_id)
            self._complete_run(
                run_id,
                SubtitlePipelinePhase.CANCELED,
                close_progress=False,
            )
            return

        if self._pipeline_state.service_state == SubtitleServiceState.SHUTTING_DOWN:
            self._complete_run(
                run_id,
                SubtitlePipelinePhase.CANCELED,
                close_progress=False,
            )
            return

        if run.subtitle_options is None:
            logger.warning("CUDA runtime install finished without pending subtitle generation options | run_id=%s", run.run_id)
            self._complete_run(
                run_id,
                SubtitlePipelinePhase.FAILED,
                close_progress=False,
            )
            show_cuda_runtime_install_failed(
                self._parent,
                "GPU runtime installation finished without subtitle options.",
            )
            return

        self._launch_subtitle_generation(run, run.subtitle_options)

    def _on_cuda_runtime_install_failed(self, run_id: int, error_text: str):
        run = self._require_active_run(run_id, "CUDA runtime install failed")
        if run is None:
            return

        logger.error("CUDA runtime install failed | run_id=%s | message=%s", run.run_id, error_text)
        self._complete_run(
            run_id,
            SubtitlePipelinePhase.FAILED,
            close_progress=True,
        )

        if self._pipeline_state.service_state == SubtitleServiceState.SHUTTING_DOWN:
            return

        show_cuda_runtime_install_failed(self._parent, error_text)

    def _on_cuda_runtime_install_canceled(self, run_id: int):
        run = self._require_active_run(run_id, "CUDA runtime install canceled")
        if run is None:
            return

        logger.info("CUDA runtime install canceled | run_id=%s", run.run_id)
        self._complete_run(
            run_id,
            SubtitlePipelinePhase.CANCELED,
            close_progress=True,
        )

        if self._pipeline_state.service_state == SubtitleServiceState.SHUTTING_DOWN:
            return

        show_cuda_runtime_install_canceled(self._parent)

    def _show_generation_success_outcome(
        self,
        output_path: str,
        auto_open_outcome: SubtitleAutoOpenOutcome,
        *,
        used_fallback_output_path: bool,
        requested_output_path: str | None,
    ):
        if auto_open_outcome == SubtitleAutoOpenOutcome.CONTEXT_CHANGED:
            show_subtitle_created_not_loaded_due_to_context_change(self._parent, output_path)
            return

        if auto_open_outcome == SubtitleAutoOpenOutcome.LOAD_FAILED:
            show_subtitle_auto_load_failed(self._parent, output_path)
            return

        if used_fallback_output_path:
            show_subtitle_created_with_fallback_name(
                self._parent,
                requested_output_path or output_path,
                output_path,
            )
            return

        show_subtitle_created(self._parent, output_path)

    def _capture_current_generation_context(self) -> SubtitleGenerationContext | None:
        media_path = self._player.playback.current_media_path()
        if not media_path:
            return None

        return SubtitleGenerationContext(
            media_path=media_path,
            request_id=self._player.playback.current_request_id(),
        )

    def _apply_overwrite_confirmation(
        self,
        options: SubtitleGenerationDialogResult,
        validation_result: SubtitleGenerationValidationResult,
    ) -> SubtitleGenerationDialogResult:
        if validation_result.reason != SubtitleGenerationValidationFailure.OVERWRITE_CONFIRMATION_REQUIRED:
            return options

        output_path = validation_result.output_path or options.output_path
        return replace(
            options,
            overwrite_confirmed_for_path=self._normalize_output_path_for_confirmation(output_path),
        )

    def _normalize_output_path_for_confirmation(self, output_path: str) -> str:
        try:
            return os.path.normcase(str(Path(output_path).expanduser().resolve(strict=False)))
        except (OSError, RuntimeError, ValueError):
            return str(output_path)

    def _discard_starting_run(self, reason: str):
        self._assert_pipeline_thread()
        run = self._pipeline_state.active_run
        if run is not None:
            logger.debug("Discarding starting subtitle pipeline run | run_id=%s | reason=%s", run.run_id, reason)
            self._clear_subtitle_runtime(run)
        self._pipeline_state.discard_active_run()
        self._transition_back_to_dialog(reason)

    def _clear_subtitle_runtime(self, run: SubtitlePipelineRun):
        self._assert_pipeline_thread()
        run.subtitle_thread = None
        run.subtitle_worker = None
        run.subtitle_cancel_requested = False

    def _run_is_terminal(self, run: SubtitlePipelineRun) -> bool:
        return run.phase in (
            SubtitlePipelinePhase.SUCCEEDED,
            SubtitlePipelinePhase.FAILED,
            SubtitlePipelinePhase.CANCELED,
        )

    def _run_is_waiting_for_thread_cleanup(self, run: SubtitlePipelineRun) -> bool:
        if run.task == SubtitlePipelineTask.SUBTITLE_GENERATION:
            return run.run_id in self._pending_subtitle_thread_run_ids
        if run.task == SubtitlePipelineTask.CUDA_INSTALL:
            return self._cuda_runtime_flow.is_active()
        return False

    def _transition_back_to_dialog(self, reason: str):
        self._assert_pipeline_thread()
        self._pipeline_state.transition_service_state(
            SubtitleServiceState.DIALOG_OPEN,
            reason,
            allowed=(SubtitleServiceState.DIALOG_OPEN,),
        )

    def _complete_run(
        self,
        run_id: int,
        terminal_phase: SubtitlePipelinePhase,
        *,
            close_progress: bool,
    ):
        self._assert_pipeline_thread()
        run = self._pipeline_state.active_run if self._pipeline_state.active_run is not None and self._pipeline_state.active_run.run_id == run_id else None
        if run is None:
            logger.debug("Ignoring terminal transition for stale subtitle pipeline run | run_id=%s", run_id)
            return

        log_timing(
            logger,
            "Subtitle timing",
            "job_total",
            elapsed_ms_since(run.started_at),
            run_id=run.run_id,
            media=run.context.media_path,
            output=(run.subtitle_options or run.requested_options).output_path,
            result=terminal_phase.name.lower(),
        )

        is_shutdown = self._pipeline_state.service_state == SubtitleServiceState.SHUTTING_DOWN

        if close_progress:
            self._ui.close_progress_dialog()

        clear_active_run = not self._run_is_waiting_for_thread_cleanup(run)
        if clear_active_run:
            self._clear_subtitle_runtime(run)
        self._pipeline_state.complete_run(
            run,
            terminal_phase,
            clear_active_run=clear_active_run,
            record_result=not is_shutdown,
        )
        if not is_shutdown:
            self._pipeline_state.transition_service_state(
                SubtitleServiceState.IDLE,
                f"record terminal result for run {run_id}",
                allowed=(SubtitleServiceState.IDLE, SubtitleServiceState.DIALOG_OPEN),
            )
        self._release_playback_takeover(resume_playback=not is_shutdown)
        self._complete_shutdown_if_possible()

    def _suspend_player_ui_for_generation(self):
        if self._player_ui_suspend_lease is not None:
            return

        self._player_ui_suspend_lease = self._player.suspend_for_subtitle_generation()

    def _release_playback_takeover(self, *, resume_playback: bool):
        player_ui_suspend_lease = self._player_ui_suspend_lease
        self._player_ui_suspend_lease = None
        if player_ui_suspend_lease is not None:
            player_ui_suspend_lease.release()

        self._playback_takeover.release(resume_playback=resume_playback)

    def _is_current_run_event(self, run_id: int, event_name: str) -> bool:
        if self._pipeline_state.active_run is None:
            logger.debug("Ignoring %s for stale subtitle pipeline run | run_id=%s | active_run=<none>", event_name, run_id)
            return False

        if self._pipeline_state.active_run.run_id != run_id:
            logger.debug(
                "Ignoring %s for stale subtitle pipeline run | run_id=%s | active_run=%s",
                event_name,
                run_id,
                self._pipeline_state.active_run.run_id,
            )
            return False

        return True

    def _require_active_run(self, run_id: int, event_name: str) -> SubtitlePipelineRun | None:
        if not self._is_current_run_event(run_id, event_name):
            return None
        return self._pipeline_state.active_run

    def _assert_pipeline_thread(self):
        app = QCoreApplication.instance()
        if app is None:
            return

        if self._is_pipeline_thread(
            is_main_thread=QThread.isMainThread(),
            service_thread=self.thread(),
            app_thread=app.thread(),
        ):
            return

        message = "SubtitleGenerationService pipeline state must be mutated from its Qt owner thread"
        logger.critical(message)
        raise RuntimeError(message)

    @staticmethod
    def _is_pipeline_thread(*, is_main_thread: bool, service_thread, app_thread) -> bool:
        return is_main_thread and service_thread == app_thread

    def _current_run_id(self) -> int | None:
        if self._pipeline_state.active_run is None:
            return None
        return self._pipeline_state.active_run.run_id

    def _log_dialog_confirm_timing(self, output_path: str):
        if self._dialog_request_started_at is None:
            return

        log_timing(
            logger,
            "Subtitle timing",
            "dialog_confirm",
            elapsed_ms_since(self._dialog_request_started_at),
            media=self._dialog_request_media_path,
            output=output_path,
        )
        self._clear_dialog_request_timing()

    def _clear_dialog_request_timing(self):
        self._dialog_request_started_at = None
        self._dialog_request_media_path = None
