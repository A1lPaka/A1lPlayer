import logging
import os
import signal
import subprocess
import threading
from typing import IO


logger = logging.getLogger(__name__)


class SubprocessLifecycleMixin:
    _GRACEFUL_CANCEL_TIMEOUT_SECONDS = 1.5

    def _init_subprocess_lifecycle(self):
        self._process: subprocess.Popen[str] | None = None
        self._termination_lock = threading.Lock()
        self._termination_started = False
        self._terminating_process: subprocess.Popen[str] | None = None
        self._force_stop_requested = False

    def _begin_termination(self):
        with self._termination_lock:
            if self._termination_started:
                return
            process = self._process
            self._termination_started = True
            self._terminating_process = process

        threading.Thread(
            target=self._terminate_process_lifecycle,
            args=(process,),
            daemon=True,
        ).start()

    def _terminate_process_lifecycle(self, process: subprocess.Popen[str] | None):
        try:
            if process is None or process.poll() is not None:
                return

            if self._force_stop_requested:
                self._kill_process_tree(process)
                return

            try:
                self._request_graceful_stop(process)
            except Exception:
                logger.exception(
                    "Failed to request graceful stop for %s; escalating to hard kill | pid=%s",
                    self._subprocess_log_name(),
                    process.pid,
                )
            else:
                try:
                    process.wait(timeout=self._graceful_cancel_timeout_seconds())
                    return
                except subprocess.TimeoutExpired:
                    if self._force_stop_requested:
                        self._kill_process_tree(process)
                        return
                    logger.warning(
                        "%s did not stop gracefully in time; escalating to hard kill | pid=%s",
                        self._subprocess_log_name().capitalize(),
                        process.pid,
                    )

            if process.poll() is None:
                self._kill_process_tree(process)
        except Exception:
            logger.exception(
                "Failed to terminate %s cleanly | pid=%s",
                self._subprocess_log_name(),
                process.pid if process is not None else "<unknown>",
            )
        finally:
            with self._termination_lock:
                if self._terminating_process is process:
                    self._termination_started = False
                    self._terminating_process = None

    def _graceful_cancel_timeout_seconds(self) -> float:
        return float(self._GRACEFUL_CANCEL_TIMEOUT_SECONDS)

    def _request_graceful_stop(self, process: subprocess.Popen[str]):
        process.terminate()

    def _kill_process_tree(self, process: subprocess.Popen[str]):
        if process.poll() is not None:
            return

        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return

        os.killpg(os.getpgid(process.pid), signal.SIGKILL)

    def _subprocess_spawn_options(self) -> dict:
        if os.name == "nt":
            return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        return {"start_new_session": True}

    def _close_stream(self, stream: IO[str] | None):
        if stream is None:
            return
        try:
            stream.close()
        except OSError:
            logger.debug(
                "Best-effort stream close failed for %s",
                self._subprocess_log_name(),
                exc_info=True,
            )

    def _subprocess_log_name(self) -> str:
        return "worker subprocess"
