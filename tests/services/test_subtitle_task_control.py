from models import SubtitleGenerationDialogResult
from services.subtitles.SubtitlePipelineState import (
    SubtitleGenerationContext,
    SubtitlePipelinePhase,
    SubtitlePipelineRun,
    SubtitlePipelineTask,
)
from services.subtitles.SubtitleTaskControl import CudaRuntimeTaskControl, SubtitleWorkerTaskControl


class _Worker:
    def __init__(self):
        self.cancel_calls = 0
        self.force_stop_calls = 0

    def cancel(self):
        self.cancel_calls += 1

    def force_stop(self):
        self.force_stop_calls += 1


class _CudaFlow:
    def __init__(self):
        self.request_stop_calls = []
        self.active = False

    def request_stop(self, *, force: bool):
        self.request_stop_calls.append(force)
        return True

    def is_active(self):
        return self.active


def _options() -> SubtitleGenerationDialogResult:
    return SubtitleGenerationDialogResult(
        audio_stream_index=None,
        audio_language=None,
        device=None,
        model_size="small",
        output_format="srt",
        output_path="C:/media/movie.srt",
        auto_open_after_generation=True,
    )


def _run(task: SubtitlePipelineTask) -> SubtitlePipelineRun:
    run = SubtitlePipelineRun(
        run_id=7,
        context=SubtitleGenerationContext(media_path="C:/media/movie.mkv", request_id=3),
        requested_options=_options(),
        phase=SubtitlePipelinePhase.RUNNING,
    )
    run.task = task
    return run


def test_subtitle_worker_task_control_cancel_is_direct_and_idempotent():
    run = _run(SubtitlePipelineTask.SUBTITLE_GENERATION)
    worker = _Worker()
    run.subtitle_worker = worker
    control = SubtitleWorkerTaskControl(run, pending_thread_run_ids=set())

    assert control.request_stop(force=False) is True
    assert control.request_stop(force=False) is False

    assert worker.cancel_calls == 1
    assert run.subtitle_cancel_requested is True


def test_subtitle_worker_task_control_force_stop():
    run = _run(SubtitlePipelineTask.SUBTITLE_GENERATION)
    worker = _Worker()
    run.subtitle_worker = worker
    control = SubtitleWorkerTaskControl(run, pending_thread_run_ids=set())

    assert control.request_stop(force=True) is True

    assert worker.force_stop_calls == 1


def test_task_controls_report_activity():
    subtitle_run = _run(SubtitlePipelineTask.SUBTITLE_GENERATION)
    subtitle_control = SubtitleWorkerTaskControl(subtitle_run, pending_thread_run_ids={subtitle_run.run_id})
    cuda_run = _run(SubtitlePipelineTask.CUDA_INSTALL)
    cuda_flow = _CudaFlow()
    cuda_control = CudaRuntimeTaskControl(cuda_run, cuda_flow)

    assert subtitle_control.is_active() is True
    assert cuda_control.is_active() is True

    cuda_run.phase = SubtitlePipelinePhase.SUCCEEDED
    cuda_flow.active = True

    assert cuda_control.is_active() is True


def test_cuda_runtime_task_control_delegates_stop():
    run = _run(SubtitlePipelineTask.CUDA_INSTALL)
    cuda_flow = _CudaFlow()
    control = CudaRuntimeTaskControl(run, cuda_flow)

    assert control.request_stop(force=False) is True
    assert control.request_stop(force=True) is True

    assert cuda_flow.request_stop_calls == [False, True]
