import logging

from PySide6.QtWidgets import QWidget

from services.subtitles.validation.SubtitleGenerationPreflight import (
    SubtitleGenerationValidationFailure,
    SubtitleGenerationValidationResult,
)
from ui.MessageBoxService import (
    confirm_overwrite_subtitle,
    show_audio_stream_inspection_failed,
    show_audio_stream_no_longer_available,
    show_audio_streams_still_loading,
    show_choose_output_path_first,
    show_no_audio_streams_found,
    show_subtitle_output_path_unavailable,
)


logger = logging.getLogger(__name__)


class SubtitleGenerationValidationPresenter:
    def __init__(self, parent: QWidget):
        self._parent = parent

    def confirm_or_show_failure(self, result: SubtitleGenerationValidationResult) -> bool:
        if result.is_valid:
            return True

        if result.reason == SubtitleGenerationValidationFailure.EMPTY_OUTPUT_PATH:
            show_choose_output_path_first(self._parent)
            return False

        if result.reason == SubtitleGenerationValidationFailure.OUTPUT_PATH_UNAVAILABLE:
            show_subtitle_output_path_unavailable(
                self._parent,
                result.output_path or "",
                result.preflight_error,
            )
            return False

        if result.reason == SubtitleGenerationValidationFailure.OVERWRITE_CONFIRMATION_REQUIRED:
            output_path = result.output_path or ""
            if confirm_overwrite_subtitle(self._parent, output_path):
                return True
            logger.info("Subtitle generation overwrite declined by user | output=%s", output_path)
            return False

        if result.reason == SubtitleGenerationValidationFailure.AUDIO_STREAMS_STILL_LOADING:
            show_audio_streams_still_loading(self._parent)
            return False

        if result.reason == SubtitleGenerationValidationFailure.AUDIO_STREAM_INSPECTION_FAILED:
            show_audio_stream_inspection_failed(
                self._parent,
                result.formatted_reason or result.probe_error or "Audio stream inspection failed.",
            )
            return False

        if result.reason == SubtitleGenerationValidationFailure.NO_AUDIO_STREAMS_FOUND:
            show_no_audio_streams_found(self._parent)
            return False

        if result.reason == SubtitleGenerationValidationFailure.AUDIO_STREAM_NO_LONGER_AVAILABLE:
            show_audio_stream_no_longer_available(self._parent)
            return False

        logger.warning("Subtitle generation preflight failed with unknown reason | reason=%s", result.reason)
        return False
