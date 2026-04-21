import logging


logger = logging.getLogger(__name__)


def invoke_worker_method(worker, method_name: str) -> bool:
    method = getattr(worker, method_name, None)
    if method is None:
        logger.warning("Worker stop method is unavailable | method=%s", method_name)
        return False

    method()
    return True
