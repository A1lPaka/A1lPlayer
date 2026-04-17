from PySide6.QtWidgets import QFileDialog, QWidget

from services.MediaPathService import (
    MEDIA_EXTENSIONS,
    SUBTITLE_EXTENSIONS,
    build_file_dialog_filter,
)


class MediaDialogs:
    MEDIA_FILTER = build_file_dialog_filter("Media Files", MEDIA_EXTENSIONS)
    SUBTITLE_FILTER = build_file_dialog_filter("Subtitle Files", SUBTITLE_EXTENSIONS)

    def __init__(self, parent: QWidget):
        self._parent = parent

    def choose_media_files(self, initial_dir: str) -> list[str]:
        file_paths, _ = QFileDialog.getOpenFileNames(
            self._parent,
            "Open Media Files",
            initial_dir,
            self.MEDIA_FILTER,
        )
        return file_paths

    def choose_media_folder(self, initial_dir: str) -> str:
        return QFileDialog.getExistingDirectory(
            self._parent,
            "Open Media Folder",
            initial_dir,
        )

    def choose_subtitle_file(self, initial_dir: str) -> str:
        subtitle_path, _ = QFileDialog.getOpenFileName(
            self._parent,
            "Open Subtitle",
            initial_dir,
            self.SUBTITLE_FILTER,
        )
        return subtitle_path
