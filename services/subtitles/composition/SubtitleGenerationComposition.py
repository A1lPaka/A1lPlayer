from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QWidget

from models.SubtitleGenerationDialogResult import SubtitleGenerationDialogResult
from services.app.MediaSettingsStore import MediaSettingsStore
from services.subtitles.application.SubtitleGenerationCompletionFlow import SubtitleGenerationCompletionFlow
from services.subtitles.application.SubtitleGenerationRuntimeCoordinator import SubtitleGenerationRuntimeCoordinator
from services.subtitles.application.SubtitleGenerationStartFlow import SubtitleGenerationStartFlow
from services.subtitles.presentation.SubtitleGenerationOutcomePresenter import SubtitleGenerationOutcomePresenter
from services.subtitles.presentation.SubtitleGenerationUiCoordinator import SubtitleGenerationUiCoordinator
from services.subtitles.presentation.SubtitleGenerationValidationPresenter import SubtitleGenerationValidationPresenter
from services.subtitles.state.SubtitlePipelineState import (
    SubtitlePipelinePhase,
    SubtitlePipelineRun,
    SubtitlePipelineStateMachine,
)
from services.subtitles.state.SubtitlePipelineTransitions import SubtitlePipelineTransitions
from services.subtitles.state.SubtitleShutdownCoordinator import SubtitleShutdownCoordinator
from services.subtitles.validation.SubtitleGenerationPreflight import SubtitleGenerationPreflight
from services.subtitles.workers.SubtitleCudaRuntimeFlow import SubtitleCudaRuntimeFlow
from services.subtitles.workers.SubtitleGenerationAudioProbeFlow import SubtitleGenerationAudioProbeFlow
from services.subtitles.workers.SubtitleGenerationJobRunner import SubtitleGenerationJobRunner
from services.subtitles.workers.SubtitleWhisperModelFlow import SubtitleWhisperModelFlow
from ui.PlayerWindow import PlayerWindow

if TYPE_CHECKING:
    from services.media.MediaLibraryService import MediaLibraryService


@dataclass
class SubtitleGenerationCompositionCallbacks:
    current_theme_color: Callable[[], Any]
    dialog_media_path_for_audio_probe: Callable[[], str | None]
    dialog_lifecycle_state_name: Callable[[], str]
    complete_run: Callable[[int, SubtitlePipelinePhase, bool], None]
    launch_subtitle_generation: Callable[[SubtitlePipelineRun, SubtitleGenerationDialogResult], None]
    retry_whisper_model_install: Callable[[SubtitlePipelineRun, str], None]
    can_start_subtitle_worker: Callable[[int, QThread, Any], bool]
    on_subtitle_worker_start_aborted: Callable[[int, QThread, Any], None]
    suspend_player_ui_for_generation: Callable[[], None]
    on_worker_status_changed_from_worker: Callable[[str], None]
    on_worker_progress_changed_from_worker: Callable[[int], None]
    on_worker_details_changed_from_worker: Callable[[str], None]
    on_subtitle_generation_finished_from_worker: Callable[[str, bool, bool], None]
    on_subtitle_generation_failed_from_worker: Callable[[str, str], None]
    on_subtitle_generation_canceled_from_worker: Callable[[], None]
    on_subtitle_worker_thread_finished: Callable[[int], None]
    assert_pipeline_thread: Callable[[], None]
    emit_shutdown_finished: Callable[[], None]
    request_active_task_stop: Callable[..., None]
    complete_shutdown_if_possible: Callable[[], None]
    on_worker_status_changed: Callable[[int, str], None]
    on_worker_details_changed: Callable[[int, str], None]
    on_cuda_runtime_flow_thread_finished: Callable[[int], None]
    on_whisper_model_flow_thread_finished: Callable[[int], None]
    log_dialog_confirm_timing: Callable[[str], None]


@dataclass
class SubtitleGenerationComposition:
    ui: SubtitleGenerationUiCoordinator
    outcome_presenter: SubtitleGenerationOutcomePresenter
    preflight: SubtitleGenerationPreflight
    validation_presenter: SubtitleGenerationValidationPresenter
    pipeline_state: SubtitlePipelineStateMachine
    transitions: SubtitlePipelineTransitions
    shutdown: SubtitleShutdownCoordinator
    audio_probe_flow: SubtitleGenerationAudioProbeFlow
    cuda_runtime_flow: SubtitleCudaRuntimeFlow
    whisper_model_flow: SubtitleWhisperModelFlow
    completion_flow: SubtitleGenerationCompletionFlow
    subtitle_job_runner: SubtitleGenerationJobRunner
    runtime: SubtitleGenerationRuntimeCoordinator
    start_flow: SubtitleGenerationStartFlow

    @classmethod
    def create(
        cls,
        *,
        parent: QWidget,
        player: PlayerWindow,
        store: MediaSettingsStore,
        media_library: "MediaLibraryService",
        callbacks: SubtitleGenerationCompositionCallbacks,
    ) -> "SubtitleGenerationComposition":
        ui = SubtitleGenerationUiCoordinator(
            parent,
            theme_color_getter=callbacks.current_theme_color,
        )
        outcome_presenter = SubtitleGenerationOutcomePresenter(parent)
        preflight = SubtitleGenerationPreflight(parent)
        validation_presenter = SubtitleGenerationValidationPresenter(parent)
        pipeline_state = SubtitlePipelineStateMachine()
        transitions = SubtitlePipelineTransitions(pipeline_state)
        shutdown = SubtitleShutdownCoordinator(pipeline_state, transitions)
        audio_probe_flow = SubtitleGenerationAudioProbeFlow(
            parent,
            player,
            ui,
            preflight,
            is_generation_dialog_open=pipeline_state.has_dialog_open,
            dialog_media_path=callbacks.dialog_media_path_for_audio_probe,
            dialog_lifecycle_state_name=callbacks.dialog_lifecycle_state_name,
        )
        cuda_runtime_flow = SubtitleCudaRuntimeFlow(parent)
        whisper_model_flow = SubtitleWhisperModelFlow(parent)

        completion_flow = SubtitleGenerationCompletionFlow(
            store=store,
            media_library=media_library,
            ui=ui,
            transitions=transitions,
            outcome_presenter=outcome_presenter,
            complete_run=callbacks.complete_run,
            launch_subtitle_generation=callbacks.launch_subtitle_generation,
            retry_model_install=callbacks.retry_whisper_model_install,
        )
        cls._connect_background_flows(
            callbacks,
            audio_probe_flow=audio_probe_flow,
            cuda_runtime_flow=cuda_runtime_flow,
            whisper_model_flow=whisper_model_flow,
            completion_flow=completion_flow,
        )

        subtitle_job_runner = SubtitleGenerationJobRunner(
            parent,
            can_start_worker=callbacks.can_start_subtitle_worker,
            on_start_aborted=callbacks.on_subtitle_worker_start_aborted,
            suspend_before_start=callbacks.suspend_player_ui_for_generation,
            on_status_changed=callbacks.on_worker_status_changed_from_worker,
            on_progress_changed=callbacks.on_worker_progress_changed_from_worker,
            on_details_changed=callbacks.on_worker_details_changed_from_worker,
            on_finished=callbacks.on_subtitle_generation_finished_from_worker,
            on_failed=callbacks.on_subtitle_generation_failed_from_worker,
            on_canceled=callbacks.on_subtitle_generation_canceled_from_worker,
        )
        subtitle_job_runner.thread_finished.connect(callbacks.on_subtitle_worker_thread_finished)

        runtime = SubtitleGenerationRuntimeCoordinator(
            player=player,
            ui=ui,
            pipeline_state=pipeline_state,
            transitions=transitions,
            shutdown=shutdown,
            audio_probe_flow=audio_probe_flow,
            cuda_runtime_flow=cuda_runtime_flow,
            whisper_model_flow=whisper_model_flow,
            subtitle_job_runner=subtitle_job_runner,
            assert_pipeline_thread=callbacks.assert_pipeline_thread,
            on_shutdown_finished=callbacks.emit_shutdown_finished,
        )

        start_flow = SubtitleGenerationStartFlow(
            parent=parent,
            player=player,
            ui=ui,
            preflight=preflight,
            validation_presenter=validation_presenter,
            audio_probe_flow=audio_probe_flow,
            pipeline_state=pipeline_state,
            transitions=transitions,
            cuda_runtime_flow=cuda_runtime_flow,
            whisper_model_flow=whisper_model_flow,
            outcome_presenter=outcome_presenter,
            assert_pipeline_thread=callbacks.assert_pipeline_thread,
            log_dialog_confirm_timing=callbacks.log_dialog_confirm_timing,
            launch_subtitle_generation=callbacks.launch_subtitle_generation,
            complete_run=callbacks.complete_run,
            request_active_task_stop=callbacks.request_active_task_stop,
        )

        return cls(
            ui=ui,
            outcome_presenter=outcome_presenter,
            preflight=preflight,
            validation_presenter=validation_presenter,
            pipeline_state=pipeline_state,
            transitions=transitions,
            shutdown=shutdown,
            audio_probe_flow=audio_probe_flow,
            cuda_runtime_flow=cuda_runtime_flow,
            whisper_model_flow=whisper_model_flow,
            completion_flow=completion_flow,
            subtitle_job_runner=subtitle_job_runner,
            runtime=runtime,
            start_flow=start_flow,
        )

    @staticmethod
    def _connect_background_flows(
        callbacks: SubtitleGenerationCompositionCallbacks,
        *,
        audio_probe_flow: SubtitleGenerationAudioProbeFlow,
        cuda_runtime_flow: SubtitleCudaRuntimeFlow,
        whisper_model_flow: SubtitleWhisperModelFlow,
        completion_flow: SubtitleGenerationCompletionFlow,
    ) -> None:
        audio_probe_flow.thread_finished.connect(callbacks.complete_shutdown_if_possible)
        cuda_runtime_flow.status_changed.connect(callbacks.on_worker_status_changed)
        cuda_runtime_flow.details_changed.connect(callbacks.on_worker_details_changed)
        cuda_runtime_flow.finished.connect(completion_flow.handle_cuda_runtime_install_finished)
        cuda_runtime_flow.failed.connect(completion_flow.handle_cuda_runtime_install_failed)
        cuda_runtime_flow.canceled.connect(completion_flow.handle_cuda_runtime_install_canceled)
        cuda_runtime_flow.thread_finished.connect(callbacks.on_cuda_runtime_flow_thread_finished)
        whisper_model_flow.status_changed.connect(callbacks.on_worker_status_changed)
        whisper_model_flow.details_changed.connect(callbacks.on_worker_details_changed)
        whisper_model_flow.finished.connect(completion_flow.handle_model_install_finished)
        whisper_model_flow.failed.connect(completion_flow.handle_model_install_failed)
        whisper_model_flow.canceled.connect(completion_flow.handle_model_install_canceled)
        whisper_model_flow.thread_finished.connect(callbacks.on_whisper_model_flow_thread_finished)
