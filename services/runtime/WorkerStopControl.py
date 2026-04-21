import logging


logger = logging.getLogger(__name__)


def call_worker_stop(worker, method_name: str) -> bool:
    method = getattr(worker, method_name, None)
    if method is None:
        logger.warning("Worker stop method is unavailable | method=%s", method_name)
        return False

    method()
    return True
