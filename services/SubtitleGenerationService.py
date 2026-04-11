import logging
from dataclasses import dataclass, replace
from enum import Enum, auto

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import QWidget

from services.MediaSettingsStore import MediaSettingsStore
from services.SubtitleCudaRuntimeFlow import SubtitleCudaRuntimeFlow
from services.SubtitleGenerationOutcomeHandler import (
    SubtitleAutoOpenOutcome,
    SubtitleGenerationOutcomeHandler,
)
from services.SubtitleGenerationPreflight import SubtitleGenerationPreflight
from services.SubtitleGenerationUiCoordinator import SubtitleGenerationUiCoordinator
from services.SubtitleGenerationWorkers import SubtitleGenerationWorker
from services.SubtitleMaker import (
    get_missing_windows_cuda_runtime_packages,
)
from ui.MessageBoxService import (
    prompt_cuda_runtime_choice,
    show_subtitle_generation_already_running,
)
from ui.PlayerWindow import PlayerWindow
from ui.SubtitleGenerationDialog import SubtitleGenerationDialogResult


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubtitleGenerationContext:
    media_path: str
    request_id: int | None


class SubtitleGenerationState(Enum):
    IDLE = auto()
    DIALOG_OPEN = auto()
    STARTING = auto()
    RUNNING = auto()
    CANCELING = auto()
    SHUTTING_DOWN = auto()
    SUCCEEDED = auto()
    FAILED = auto()
    CANCELED = auto()


class SubtitlePipelineTask(Enum):
    NONE = auto()
    CUDA_INSTALL = auto()
    SUBTITLE_GENERATION = auto()


@dataclass
class SubtitlePipelineRun:
    run_id: int
    context: SubtitleGenerationContext
    requested_options: SubtitleGenerationDialogResult
    subtitle_options: SubtitleGenerationDialogResult | None = None
    task: SubtitlePipelineTask = SubtitlePipelineTask.NONE


class SubtitleGenerationService(QObject):
    shutdown_finished = Signal()

    def __init__(
        self,
        parent: QWidget,
        player_window: PlayerWindow,
        store: MediaSettingsStore,
    ):
        super().__init__(parent)
        self._parent = parent
        self._player = player_window
        self._store = store
        self._ui = SubtitleGenerationUiCoordinator(
            parent,
            theme_color_getter=lambda: self._player.theme_color,
        )
        self._preflight = SubtitleGenerationPreflight(parent)
        self._outcomes = SubtitleGenerationOutcomeHandler(parent)
        self._state = SubtitleGenerationState.IDLE
        self._active_run: SubtitlePipelineRun | None = None
        self._next_run_id = 1
        self._shutdown_completed = False
        self._force_shutdown_requested = False
        self._player_ui_suspended = False
        self._subtitle_thread: QThread | None = None
        self._subtitle_worker: SubtitleGenerationWorker | None = None
        self._subtitle_cancel_requested = False
        self._cuda_runtime_flow = SubtitleCudaRuntimeFlow(parent, self._ui)
        self._cuda_runtime_flow.status_changed.connect(self._on_cuda_flow_status_changed)
        self._cuda_runtime_flow.details_changed.connect(self._on_cuda_flow_details_changed)
        self._cuda_runtime_flow.finished.connect(self._on_cuda_runtime_install_finished)
        self._cuda_runtime_flow.failed.connect(self._on_cuda_runtime_install_failed)
        self._cuda_runtime_flow.canceled.connect(self._on_cuda_runtime_install_canceled)
        self._cuda_runtime_flow.thread_finished.connect(self._on_cuda_runtime_flow_thread_finished)

    def generate_subtitle(self) -> bool:
        current_media_path = self._player.playback.current_media_path()
        if not current_media_path:
            logger.info("Subtitle generation requested without an active media item")
            return False

        if self._shutdown_completed or self._state == SubtitleGenerationState.SHUTTING_DOWN:
            logger.info("Subtitle generation request ignored because shutdown is in progress")
            return False

        if self._state == SubtitleGenerationState.DIALOG_OPEN:
            self._ui.focus_active_dialog()
            logger.info("Subtitle generation request focused the existing generation dialog")
            return False

        if self.has_active_tasks() or self._state in (
            SubtitleGenerationState.STARTING,
            SubtitleGenerationState.RUNNING,
            SubtitleGenerationState.CANCELING,
        ):
            self._ui.focus_active_dialog()
            show_subtitle_generation_already_running(self._parent)
            logger.info("Subtitle generation request ignored because another background task is running")
            return False

        if self._player.playback.is_playing():
            self._player.pause()

        if not self._transition_state(
            SubtitleGenerationState.DIALOG_OPEN,
            "open generation dialog",
            allowed=(
                SubtitleGenerationState.IDLE,
                SubtitleGenerationState.SUCCEEDED,
                SubtitleGenerationState.FAILED,
                SubtitleGenerationState.CANCELED,
            ),
        ):
            return False

        self._ui.open_generation_dialog(
            current_media_path,
            self._build_generation_audio_tracks(),
            on_generate=self._start_subtitle_generation,
            on_cancel=self._on_generation_dialog_canceled,
        )
        return True

    def _build_generation_audio_tracks(self) -> list[tuple[int | None, str]]:
        media_path = self._player.playback.current_media_path()
        return self._preflight.build_generation_audio_tracks(media_path)

    def _start_subtitle_generation(self, options: SubtitleGenerationDialogResult):
        if not self._transition_state(
            SubtitleGenerationState.STARTING,
            "start subtitle generation",
            allowed=(SubtitleGenerationState.DIALOG_OPEN,),
        ):
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

        run = self._begin_pipeline_run(generation_context, options)
        validation_result = self._preflight.validate_generation_request(current_media_path, options)
        if not validation_result.is_valid:
            self._discard_starting_run("subtitle generation preflight failed")
            return

        resolved_options = self._resolve_cuda_runtime_options(options, run)
        if resolved_options is None:
            if self._state == SubtitleGenerationState.STARTING:
                self._discard_starting_run("subtitle launch postponed or canceled during CUDA resolution")
            return

        run.subtitle_options = resolved_options
        self._ensure_player_ui_suspended()
        self._launch_subtitle_generation(run, resolved_options)

    def _launch_subtitle_generation(
        self,
        run: SubtitlePipelineRun,
        options: SubtitleGenerationDialogResult,
    ):
        if not self._transition_state(
            SubtitleGenerationState.RUNNING,
            "launch subtitle generation worker",
            allowed=(SubtitleGenerationState.STARTING, SubtitleGenerationState.RUNNING),
        ):
            return

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
        self._ui.open_generation_progress(options, on_cancel=self._cancel_subtitle_generation)

        thread = QThread(self._parent)
        worker = SubtitleGenerationWorker(run.context.media_path, options)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.status_changed.connect(lambda text, run_id=run.run_id: self._on_worker_status_changed(run_id, text))
        worker.progress_changed.connect(lambda value, run_id=run.run_id: self._on_worker_progress_changed(run_id, value))
        worker.details_changed.connect(lambda text, run_id=run.run_id: self._on_worker_details_changed(run_id, text))
        worker.finished.connect(
            lambda output_path, auto_open, run_id=run.run_id: self._on_subtitle_generation_finished(
                run_id,
                output_path,
                auto_open,
            )
        )
        worker.failed.connect(
            lambda error_text, diagnostics, run_id=run.run_id: self._on_subtitle_generation_failed(
                run_id,
                error_text,
                diagnostics,
            )
        )
        worker.canceled.connect(lambda run_id=run.run_id: self._on_subtitle_generation_canceled(run_id))

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

        self._subtitle_thread = thread
        self._subtitle_worker = worker
        self._subtitle_cancel_requested = False
        thread.start()

    def _resolve_cuda_runtime_options(
        self,
        options: SubtitleGenerationDialogResult,
        run: SubtitlePipelineRun,
    ) -> SubtitleGenerationDialogResult | None:
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
        if choice == "cancel":
            logger.info("User canceled subtitle generation after CUDA runtime prompt | run_id=%s", run.run_id)
            return None

        if choice == "cpu":
            logger.info("User switched subtitle generation from CUDA to CPU | run_id=%s", run.run_id)
            return replace(options, device="cpu")

        run.subtitle_options = options
        self._ensure_player_ui_suspended()
        self._start_cuda_runtime_install(run, missing_packages)
        return None

    def _start_cuda_runtime_install(
        self,
        run: SubtitlePipelineRun,
        missing_packages: list[str],
    ):
        if not self._transition_state(
            SubtitleGenerationState.RUNNING,
            "start CUDA runtime install",
            allowed=(SubtitleGenerationState.STARTING,),
        ):
            return

        logger.info(
            "Starting CUDA runtime install flow | run_id=%s | media=%s | request_id=%s | packages=%s",
            run.run_id,
            run.context.media_path,
            run.context.request_id,
            ", ".join(missing_packages),
        )
        run.task = SubtitlePipelineTask.CUDA_INSTALL
        if not self._cuda_runtime_flow.start(run.run_id, missing_packages):
            logger.error("CUDA runtime install flow could not be started | run_id=%s", run.run_id)
            self._complete_run(
                run.run_id,
                SubtitleGenerationState.FAILED,
                close_progress=True,
            )
            if self._state != SubtitleGenerationState.SHUTTING_DOWN:
                self._outcomes.show_cuda_install_failed("GPU runtime installation could not be started.")

    @Slot()
    def _cancel_subtitle_generation(self):
        if self._state not in (SubtitleGenerationState.RUNNING, SubtitleGenerationState.CANCELING):
            logger.debug("Subtitle generation cancel ignored because pipeline is not running | state=%s", self._state.name)
            return

        if self._subtitle_worker is None:
            logger.debug("Subtitle generation cancel ignored because no subtitle worker is active")
            return

        if self._subtitle_cancel_requested:
            logger.info("Repeated cancel request ignored for subtitle generation worker")
            return

        self._transition_state(
            SubtitleGenerationState.CANCELING,
            "cancel subtitle generation",
            allowed=(SubtitleGenerationState.RUNNING, SubtitleGenerationState.CANCELING),
        )
        self._subtitle_cancel_requested = True
        logger.info("Cancel requested for subtitle generation worker | run_id=%s", self._current_run_id())
        self._subtitle_worker.cancel()
        self._ui.show_subtitle_cancel_pending()

    @Slot()
    def _cancel_cuda_runtime_install(self):
        if self._state not in (SubtitleGenerationState.RUNNING, SubtitleGenerationState.CANCELING):
            logger.debug("CUDA runtime install cancel ignored because pipeline is not running | state=%s", self._state.name)
            return

        if not self._cuda_runtime_flow.is_active():
            logger.debug("CUDA runtime install cancel ignored because no CUDA flow is active")
            return

        self._transition_state(
            SubtitleGenerationState.CANCELING,
            "cancel CUDA runtime install",
            allowed=(SubtitleGenerationState.RUNNING, SubtitleGenerationState.CANCELING),
        )
        logger.info("Cancel requested for CUDA runtime install worker | run_id=%s", self._current_run_id())
        self._cuda_runtime_flow.cancel()

    def has_active_tasks(self) -> bool:
        return self._is_thread_active(self._subtitle_thread) or self._cuda_runtime_flow.is_active()

    def is_shutdown_in_progress(self) -> bool:
        return self._state == SubtitleGenerationState.SHUTTING_DOWN and not self._shutdown_completed

    def begin_shutdown(self) -> bool:
        if self._shutdown_completed:
            logger.debug("Subtitle generation service shutdown requested after completion")
            return False

        if self._state == SubtitleGenerationState.SHUTTING_DOWN:
            logger.info(
                "Subtitle generation service shutdown already in progress | force_requested=%s",
                self._force_shutdown_requested,
            )
            return self.has_active_tasks()

        logger.info("Subtitle generation service async shutdown started | state=%s", self._state.name)
        self._transition_state(
            SubtitleGenerationState.SHUTTING_DOWN,
            "begin graceful shutdown",
            allowed=tuple(SubtitleGenerationState),
        )
        self._force_shutdown_requested = False
        self._ui.close_generation_dialog()
        self._request_background_task_stop(force=False)
        self._complete_shutdown_if_possible()
        return self.has_active_tasks()

    def begin_force_shutdown(self) -> bool:
        if self._shutdown_completed:
            logger.debug("Subtitle generation service force shutdown requested after completion")
            return False

        if self._state != SubtitleGenerationState.SHUTTING_DOWN:
            logger.warning("Subtitle generation service force shutdown requested before graceful shutdown")
            self._transition_state(
                SubtitleGenerationState.SHUTTING_DOWN,
                "begin force shutdown",
                allowed=tuple(SubtitleGenerationState),
            )
            self._ui.close_generation_dialog()

        if self._force_shutdown_requested:
            logger.info("Repeated force shutdown request ignored for subtitle generation service")
            return self.has_active_tasks()

        logger.warning("Subtitle generation service async force shutdown started")
        self._force_shutdown_requested = True
        self._ui.close_progress_dialog()
        self._request_background_task_stop(force=True)
        self._complete_shutdown_if_possible()
        return self.has_active_tasks()

    def _request_background_task_stop(self, force: bool):
        if self._subtitle_worker is not None:
            if force:
                self._subtitle_worker.force_stop()
            elif not self._subtitle_cancel_requested:
                self._subtitle_cancel_requested = True
                logger.info("Cancel requested for subtitle generation worker during shutdown | run_id=%s", self._current_run_id())
                self._subtitle_worker.cancel()
                self._ui.show_subtitle_cancel_pending()

        if self._cuda_runtime_flow.is_active():
            logger.info(
                "Stop requested for CUDA runtime install flow during shutdown | run_id=%s | force=%s",
                self._current_run_id(),
                force,
            )
            self._cuda_runtime_flow.request_stop(force=force)

    def _finalize_shutdown_state(self):
        self._ui.close_progress_dialog()
        self._active_run = None
        self._clear_subtitle_thread_references()
        self._ensure_player_ui_resumed()
        self._shutdown_completed = True
        self._force_shutdown_requested = False

    def _is_thread_active(self, thread: QThread | None) -> bool:
        return thread is not None and thread.isRunning()

    def _complete_shutdown_if_possible(self):
        if self._state != SubtitleGenerationState.SHUTTING_DOWN or self._shutdown_completed:
            return

        if self.has_active_tasks():
            logger.debug(
                "Subtitle generation service shutdown still waiting for background tasks | subtitle_running=%s | cuda_running=%s | force_requested=%s",
                self._is_thread_active(self._subtitle_thread),
                self._cuda_runtime_flow.is_active(),
                self._force_shutdown_requested,
            )
            return

        self._finalize_shutdown_state()
        logger.info("Subtitle generation service shutdown finished")
        self.shutdown_finished.emit()

    @Slot()
    def _on_generation_dialog_canceled(self):
        if self._state != SubtitleGenerationState.DIALOG_OPEN:
            return

        logger.info("Subtitle generation dialog closed without launching a job")
        self._transition_state(
            SubtitleGenerationState.IDLE,
            "close generation dialog",
            allowed=(SubtitleGenerationState.DIALOG_OPEN,),
        )

    def _on_background_task_thread_finished(self, run_id: int, task: SubtitlePipelineTask):
        if task == SubtitlePipelineTask.SUBTITLE_GENERATION:
            self._clear_subtitle_thread_references()

        if self._active_run is not None and self._active_run.run_id == run_id:
            logger.debug(
                "Background task thread finished for active subtitle pipeline run | run_id=%s | task=%s | state=%s",
                run_id,
                task.name,
                self._state.name,
            )
        else:
            logger.debug(
                "Background task thread finished for stale subtitle pipeline run | run_id=%s | task=%s | state=%s",
                run_id,
                task.name,
                self._state.name,
            )

        self._complete_shutdown_if_possible()

    def _on_cuda_runtime_flow_thread_finished(self, run_id: int):
        self._on_background_task_thread_finished(run_id, SubtitlePipelineTask.CUDA_INSTALL)

    def _on_worker_status_changed(self, run_id: int, text: str):
        if not self._is_current_run_event(run_id, "status update"):
            return
        self._ui.update_progress_status(text)

    def _on_cuda_flow_status_changed(self, run_id: int, text: str):
        self._on_worker_status_changed(run_id, text)

    def _on_worker_progress_changed(self, run_id: int, value: int):
        if not self._is_current_run_event(run_id, "progress update"):
            return
        self._ui.update_progress(value)

    def _on_worker_details_changed(self, run_id: int, text: str):
        if not self._is_current_run_event(run_id, "details update"):
            return
        self._ui.update_progress_details(text)

    def _on_cuda_flow_details_changed(self, run_id: int, text: str):
        self._on_worker_details_changed(run_id, text)

    def _on_subtitle_generation_finished(self, run_id: int, output_path: str, auto_open: bool):
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
            SubtitleGenerationState.SUCCEEDED,
            close_progress=True,
        )

        if self._state == SubtitleGenerationState.SHUTTING_DOWN:
            return

        self._store.save_last_open_dir(output_path)

        auto_open_outcome = SubtitleAutoOpenOutcome.LOADED
        if auto_open:
            if not self._matches_active_playback_context(run.context):
                current_media_path = self._player.playback.current_media_path()
                current_request_id = self._player.playback.current_request_id()
                logger.info(
                    "Skipping subtitle auto-open because playback context changed | run_id=%s | generated_media=%s | generated_request_id=%s | active_media=%s | active_request_id=%s | output=%s",
                    run.run_id,
                    run.context.media_path,
                    run.context.request_id,
                    current_media_path or "<none>",
                    current_request_id,
                    output_path,
                )
                auto_open_outcome = SubtitleAutoOpenOutcome.CONTEXT_CHANGED
            elif not self._player.playback.open_subtitle_file(output_path):
                logger.error("Generated subtitle could not be auto-loaded into playback | run_id=%s | output=%s", run.run_id, output_path)
                auto_open_outcome = SubtitleAutoOpenOutcome.LOAD_FAILED

        self._outcomes.show_generation_success(output_path, auto_open_outcome)

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
            SubtitleGenerationState.FAILED,
            close_progress=True,
        )

        if self._state == SubtitleGenerationState.SHUTTING_DOWN:
            return

        self._outcomes.show_generation_failed(error_text)

    def _on_subtitle_generation_canceled(self, run_id: int):
        run = self._require_active_run(run_id, "subtitle generation canceled")
        if run is None:
            return

        logger.info("Subtitle generation canceled | run_id=%s", run.run_id)
        self._complete_run(
            run_id,
            SubtitleGenerationState.CANCELED,
            close_progress=True,
        )

        if self._state == SubtitleGenerationState.SHUTTING_DOWN:
            return

        self._outcomes.show_generation_canceled()

    def _on_cuda_runtime_install_finished(self, run_id: int):
        run = self._require_active_run(run_id, "CUDA runtime install finished")
        if run is None:
            return

        logger.info("CUDA runtime install flow finished | run_id=%s", run.run_id)
        self._ui.close_progress_dialog()

        if self._state == SubtitleGenerationState.CANCELING:
            logger.info("Ignoring CUDA runtime completion because pipeline cancellation is already in progress | run_id=%s", run.run_id)
            self._complete_run(
                run_id,
                SubtitleGenerationState.CANCELED,
                close_progress=False,
            )
            return

        if self._state == SubtitleGenerationState.SHUTTING_DOWN:
            self._complete_run(
                run_id,
                SubtitleGenerationState.CANCELED,
                close_progress=False,
            )
            return

        if run.subtitle_options is None:
            logger.warning("CUDA runtime install finished without pending subtitle generation options | run_id=%s", run.run_id)
            self._complete_run(
                run_id,
                SubtitleGenerationState.FAILED,
                close_progress=False,
            )
            self._outcomes.show_cuda_install_failed("GPU runtime installation finished without subtitle options.")
            return

        self._launch_subtitle_generation(run, run.subtitle_options)

    def _on_cuda_runtime_install_failed(self, run_id: int, error_text: str):
        run = self._require_active_run(run_id, "CUDA runtime install failed")
        if run is None:
            return

        logger.error("CUDA runtime install failed | run_id=%s | message=%s", run.run_id, error_text)
        self._complete_run(
            run_id,
            SubtitleGenerationState.FAILED,
            close_progress=True,
        )

        if self._state == SubtitleGenerationState.SHUTTING_DOWN:
            return

        self._outcomes.show_cuda_install_failed(error_text)

    def _on_cuda_runtime_install_canceled(self, run_id: int):
        run = self._require_active_run(run_id, "CUDA runtime install canceled")
        if run is None:
            return

        logger.info("CUDA runtime install canceled | run_id=%s", run.run_id)
        self._complete_run(
            run_id,
            SubtitleGenerationState.CANCELED,
            close_progress=True,
        )

        if self._state == SubtitleGenerationState.SHUTTING_DOWN:
            return

        self._outcomes.show_cuda_install_canceled()

    @Slot()
    def _clear_subtitle_thread_references(self, *_args):
        self._subtitle_thread = None
        self._subtitle_worker = None
        self._subtitle_cancel_requested = False

    def _capture_current_generation_context(self) -> SubtitleGenerationContext | None:
        media_path = self._player.playback.current_media_path()
        if not media_path:
            return None

        return SubtitleGenerationContext(
            media_path=media_path,
            request_id=self._player.playback.current_request_id(),
        )

    def _matches_active_playback_context(self, generation_context: SubtitleGenerationContext) -> bool:
        current_media_path = self._player.playback.current_media_path()
        if current_media_path != generation_context.media_path:
            return False

        current_request_id = self._player.playback.current_request_id()
        if generation_context.request_id is None:
            return True
        return current_request_id == generation_context.request_id

    def _begin_pipeline_run(
        self,
        generation_context: SubtitleGenerationContext,
        options: SubtitleGenerationDialogResult,
    ) -> SubtitlePipelineRun:
        run = SubtitlePipelineRun(
            run_id=self._next_run_id,
            context=generation_context,
            requested_options=options,
        )
        self._next_run_id += 1
        self._active_run = run
        return run

    def _discard_starting_run(self, reason: str):
        if self._active_run is not None:
            logger.debug("Discarding starting subtitle pipeline run | run_id=%s | reason=%s", self._active_run.run_id, reason)
        self._active_run = None
        self._transition_back_to_dialog(reason)

    def _transition_back_to_dialog(self, reason: str):
        self._transition_state(
            SubtitleGenerationState.DIALOG_OPEN,
            reason,
            allowed=(SubtitleGenerationState.STARTING, SubtitleGenerationState.DIALOG_OPEN),
        )

    def _complete_run(
        self,
        run_id: int,
        terminal_state: SubtitleGenerationState,
        *,
        close_progress: bool,
    ):
        if terminal_state not in (
            SubtitleGenerationState.SUCCEEDED,
            SubtitleGenerationState.FAILED,
            SubtitleGenerationState.CANCELED,
        ):
            raise ValueError(f"Unsupported terminal state: {terminal_state}")

        run = self._active_run if self._active_run is not None and self._active_run.run_id == run_id else None
        if run is None:
            logger.debug("Ignoring terminal transition for stale subtitle pipeline run | run_id=%s", run_id)
            return

        previous_state = self._state
        is_shutdown = previous_state == SubtitleGenerationState.SHUTTING_DOWN

        if not is_shutdown:
            self._transition_state(
                terminal_state,
                f"complete run {run_id}",
                allowed=(
                    SubtitleGenerationState.RUNNING,
                    SubtitleGenerationState.CANCELING,
                    SubtitleGenerationState.STARTING,
                ),
            )

        if close_progress:
            self._ui.close_progress_dialog()

        self._active_run = None
        self._ensure_player_ui_resumed()
        self._complete_shutdown_if_possible()

    def _ensure_player_ui_suspended(self):
        if self._player_ui_suspended:
            return
        self._player.suspend_for_subtitle_generation()
        self._player_ui_suspended = True

    def _ensure_player_ui_resumed(self):
        if not self._player_ui_suspended:
            return
        self._player.resume_after_subtitle_generation()
        self._player_ui_suspended = False

    def _is_current_run_event(self, run_id: int, event_name: str) -> bool:
        if self._active_run is None:
            logger.debug("Ignoring %s for stale subtitle pipeline run | run_id=%s | active_run=<none>", event_name, run_id)
            return False

        if self._active_run.run_id != run_id:
            logger.debug(
                "Ignoring %s for stale subtitle pipeline run | run_id=%s | active_run=%s",
                event_name,
                run_id,
                self._active_run.run_id,
            )
            return False

        return True

    def _require_active_run(self, run_id: int, event_name: str) -> SubtitlePipelineRun | None:
        if not self._is_current_run_event(run_id, event_name):
            return None
        return self._active_run

    def _transition_state(
        self,
        new_state: SubtitleGenerationState,
        reason: str,
        *,
        allowed: tuple[SubtitleGenerationState, ...],
    ) -> bool:
        if self._state not in allowed:
            logger.warning(
                "Rejected subtitle pipeline state transition | from=%s | to=%s | reason=%s",
                self._state.name,
                new_state.name,
                reason,
            )
            return False

        if self._state != new_state:
            logger.debug(
                "Subtitle pipeline state transition | from=%s | to=%s | reason=%s",
                self._state.name,
                new_state.name,
                reason,
            )
        self._state = new_state
        return True

    def _current_run_id(self) -> int | None:
        if self._active_run is None:
            return None
        return self._active_run.run_id
