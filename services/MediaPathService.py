import os
import logging

from utils import normalize_path


logger = logging.getLogger(__name__)


MEDIA_EXTENSIONS = (
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
    ".mp3", ".wav", ".flac", ".m4a", ".aac",
)
SUBTITLE_EXTENSIONS = (".srt", ".ass", ".ssa", ".sub", ".vtt")


def build_file_dialog_filter(label: str, extensions) -> str:
    patterns = " ".join(f"*{extension}" for extension in extensions)
    return f"{label} ({patterns});;All Files (*)"


class MediaPathService:
    MEDIA_EXTENSIONS = frozenset(MEDIA_EXTENSIONS)
    SUBTITLE_EXTENSIONS = frozenset(SUBTITLE_EXTENSIONS)

    def collect_media_files(self, folder_path: str) -> list[str]:
        file_paths: list[str] = []
        try:
            for entry in os.scandir(folder_path):
                if not entry.is_file():
                    continue
                _, ext = os.path.splitext(entry.name)
                if ext.lower() in self.MEDIA_EXTENSIONS:
                    file_paths.append(entry.path)
        except OSError:
            logger.exception("Failed to scan media folder | folder=%s", folder_path)
            raise

        file_paths.sort(key=lambda p: os.path.basename(p).lower())
        return file_paths

    def cheap_classify_drag_paths(self, dropped_paths: list[str]) -> dict[str, list[str]]:
        media_paths: list[str] = []
        subtitle_paths: list[str] = []

        for path in self.deduplicate_paths(dropped_paths):
            if os.path.isdir(path):
                media_paths.append(path)
                continue
            if not os.path.isfile(path):
                continue

            kind = self._classify_file_path(path)
            if kind == "media":
                media_paths.append(path)
            elif kind == "subtitle":
                subtitle_paths.append(path)

        return {
            "media_paths": media_paths,
            "subtitle_paths": subtitle_paths,
        }

    def classify_drop_paths(self, dropped_paths: list[str]) -> dict[str, list[str]]:
        media_paths: list[str] = []
        subtitle_paths: list[str] = []

        for path in self.deduplicate_paths(dropped_paths):
            try:
                if os.path.isdir(path):
                    media_paths.extend(self.collect_media_files(path))
                    continue
                if not os.path.isfile(path):
                    continue
            except OSError:
                logger.exception("Failed to inspect dropped path | path=%s", path)
                raise

            kind = self._classify_file_path(path)
            if kind == "media":
                media_paths.append(path)
            elif kind == "subtitle":
                subtitle_paths.append(path)

        return {
            "media_paths": self.deduplicate_paths(media_paths),
            "subtitle_paths": self.deduplicate_paths(subtitle_paths),
        }

    def deduplicate_paths(self, paths: list[str]) -> list[str]:
        unique_paths: list[str] = []
        seen: set[str] = set()
        for path in paths:
            normalized = normalize_path(path)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            unique_paths.append(path)
        return unique_paths

    def urls_to_local_paths(self, urls) -> list[str]:
        local_paths: list[str] = []
        for url in urls:
            if not url.isLocalFile():
                continue
            local_path = url.toLocalFile()
            if local_path:
                local_paths.append(local_path)
        return local_paths

    def are_local_file_urls(self, urls) -> bool:
        saw_url = False
        for url in urls:
            saw_url = True
            if not url.isLocalFile() or not url.toLocalFile():
                return False
        return saw_url

    def _classify_file_path(self, path: str) -> str | None:
        _, ext = os.path.splitext(path)
        ext = ext.lower()
        if ext in self.MEDIA_EXTENSIONS:
            return "media"
        if ext in self.SUBTITLE_EXTENSIONS:
            return "subtitle"
        return None
