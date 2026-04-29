from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QWidget

from services.app.MediaSettingsStore import MediaSettingsStore
from services.subtitles.application.SubtitleGenerationCompletionFlow import SubtitleGenerationCompletionFlow
from services.subtitles.application.SubtitleGenerationRuntimeCoordinator import SubtitleGenerationRuntimeCoordinator
from services.subtitles.application.SubtitleGenerationStartFlow import SubtitleGenerationStartFlow
from services.subtitles.presentation.SubtitleGenerationOutcomePresenter import SubtitleGenerationOutcomePresenter
from services.subtitles.presentation.SubtitleGenerationUiCoordinator import SubtitleGenerationUiCoordinator
from services.subtitles.presentation.SubtitleGenerationValidationPresenter import SubtitleGenerationValidationPresenter
from services.subtitles.state.SubtitlePipelineState import SubtitlePipelineStateMachine
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
        service,
    ) -> "SubtitleGenerationComposition":
        ui = SubtitleGenerationUiCoordinator(
            parent,
            theme_color_getter=service._current_theme_color,
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
            dialog_media_path=service._dialog_media_path_for_audio_probe,
            dialog_lifecycle_state_name=service._dialog_lifecycle_state_name,
        )
        cuda_runtime_flow = SubtitleCudaRuntimeFlow(parent)
        whisper_model_flow = SubtitleWhisperModelFlow(parent)

        completion_flow = SubtitleGenerationCompletionFlow(
            store=store,
            media_library=media_library,
            ui=ui,
            transitions=transitions,
            outcome_presenter=outcome_presenter,
            complete_run=service._dispatch_complete_run,
            launch_subtitle_generation=service._dispatch_launch_subtitle_generation,
            retry_model_install=lambda run, model_size: service._start_flow.retry_whisper_model_install(
                run,
                model_size,
            ),
        )
        cls._connect_background_flows(
            service,
            audio_probe_flow=audio_probe_flow,
            cuda_runtime_flow=cuda_runtime_flow,
            whisper_model_flow=whisper_model_flow,
            completion_flow=completion_flow,
        )

        subtitle_job_runner = SubtitleGenerationJobRunner(
            parent,
            can_start_worker=service._can_start_subtitle_worker,
            on_start_aborted=service._on_subtitle_worker_start_aborted,
            suspend_before_start=service._suspend_player_ui_for_generation,
            on_status_changed=service._on_worker_status_changed_from_worker,
            on_progress_changed=service._on_worker_progress_changed_from_worker,
            on_details_changed=service._on_worker_details_changed_from_worker,
            on_finished=service._on_subtitle_generation_finished_from_worker,
            on_failed=service._on_subtitle_generation_failed_from_worker,
            on_canceled=service._on_subtitle_generation_canceled_from_worker,
        )
        subtitle_job_runner.thread_finished.connect(service._on_subtitle_worker_thread_finished)

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
            assert_pipeline_thread=service._assert_pipeline_thread,
            on_shutdown_finished=service.shutdown_finished.emit,
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
            assert_pipeline_thread=service._assert_pipeline_thread,
            log_dialog_confirm_timing=service._log_dialog_confirm_timing,
            launch_subtitle_generation=service._dispatch_launch_subtitle_generation,
            complete_run=service._dispatch_complete_run,
            request_active_task_stop=service._request_active_task_stop,
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
        service,
        *,
        audio_probe_flow: SubtitleGenerationAudioProbeFlow,
        cuda_runtime_flow: SubtitleCudaRuntimeFlow,
        whisper_model_flow: SubtitleWhisperModelFlow,
        completion_flow: SubtitleGenerationCompletionFlow,
    ) -> None:
        audio_probe_flow.thread_finished.connect(service._complete_shutdown_if_possible)
        cuda_runtime_flow.status_changed.connect(service._on_worker_status_changed)
        cuda_runtime_flow.details_changed.connect(service._on_worker_details_changed)
        cuda_runtime_flow.finished.connect(completion_flow.handle_cuda_runtime_install_finished)
        cuda_runtime_flow.failed.connect(completion_flow.handle_cuda_runtime_install_failed)
        cuda_runtime_flow.canceled.connect(completion_flow.handle_cuda_runtime_install_canceled)
        cuda_runtime_flow.thread_finished.connect(service._on_cuda_runtime_flow_thread_finished)
        whisper_model_flow.status_changed.connect(service._on_worker_status_changed)
        whisper_model_flow.details_changed.connect(service._on_worker_details_changed)
        whisper_model_flow.finished.connect(completion_flow.handle_model_install_finished)
        whisper_model_flow.failed.connect(completion_flow.handle_model_install_failed)
        whisper_model_flow.canceled.connect(completion_flow.handle_model_install_canceled)
        whisper_model_flow.thread_finished.connect(service._on_whisper_model_flow_thread_finished)
