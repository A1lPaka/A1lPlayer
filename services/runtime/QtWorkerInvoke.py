import logging

from PySide6.QtCore import QObject, QMetaObject, Qt


logger = logging.getLogger(__name__)


def invoke_worker_method(worker, method_name: str) -> bool:
    if isinstance(worker, QObject):
        try:
            invoked = QMetaObject.invokeMethod(worker, method_name, Qt.QueuedConnection)
        except (RuntimeError, TypeError, ValueError):
            invoked = False

        if invoked:
            return True

        logger.warning("Queued worker method invocation failed | method=%s", method_name)
        return False


    method = getattr(worker, method_name, None)
    if method is None:
        logger.warning("Worker stop method is unavailable | method=%s", method_name)
        return False

    method()
    return True
