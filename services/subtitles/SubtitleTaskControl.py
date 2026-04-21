import logging
from collections.abc import Collection
from typing import Protocol

from services.runtime.WorkerStopControl import call_worker_stop
from services.subtitles.SubtitleCudaRuntimeFlow import SubtitleCudaRuntimeFlow
from services.subtitles.SubtitlePipelineState import SubtitlePipelineRun


logger = logging.getLogger(__name__)


class SubtitleTaskControl(Protocol):
    def request_stop(self, *, force: bool) -> bool:
        ...

    def is_active(self) -> bool:
        ...


class SubtitleWorkerTaskControl:
    def __init__(self, run: SubtitlePipelineRun, pending_thread_run_ids: Collection[int]):
        self._run = run
        self._pending_thread_run_ids = pending_thread_run_ids

    def request_stop(self, *, force: bool) -> bool:
        if self._run.subtitle_worker is None:
            logger.debug("Subtitle generation stop ignored because no worker is active | force=%s", force)
            return False

        if force:
            logger.warning("Force-stop requested for subtitle generation worker | run_id=%s", self._run.run_id)
            call_worker_stop(self._run.subtitle_worker, "force_stop")
            return True

        if self._run.subtitle_cancel_requested:
            logger.info("Repeated stop request ignored for subtitle generation worker")
            return False

        self._run.subtitle_cancel_requested = True
        logger.info("Cancel requested for subtitle generation worker | run_id=%s", self._run.run_id)
        call_worker_stop(self._run.subtitle_worker, "cancel")
        return True

    def is_active(self) -> bool:
        return self._run.run_id in self._pending_thread_run_ids or self._run.keeps_shutdown_pending()


class CudaRuntimeTaskControl:
    def __init__(self, run: SubtitlePipelineRun, cuda_runtime_flow: SubtitleCudaRuntimeFlow):
        self._run = run
        self._cuda_runtime_flow = cuda_runtime_flow

    def request_stop(self, *, force: bool) -> bool:
        return self._cuda_runtime_flow.request_stop(force=force)

    def is_active(self) -> bool:
        return self._cuda_runtime_flow.is_active() or self._run.keeps_shutdown_pending()
