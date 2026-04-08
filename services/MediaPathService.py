import os

from utils import _normalize_path


class MediaPathService:
    MEDIA_EXTENSIONS = {
        ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
        ".mp3", ".wav", ".flac", ".m4a", ".aac",
    }
    SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".sub", ".vtt"}

    def collect_media_files(self, folder_path: str) -> list[str]:
        file_paths: list[str] = []
        for entry in os.scandir(folder_path):
            if not entry.is_file():
                continue
            _, ext = os.path.splitext(entry.name)
            if ext.lower() in self.MEDIA_EXTENSIONS:
                file_paths.append(entry.path)

        file_paths.sort(key=lambda p: os.path.basename(p).lower())
        return file_paths

    def classify_drop_paths(self, dropped_paths: list[str]) -> dict[str, list[str]]:
        media_paths: list[str] = []
        subtitle_paths: list[str] = []

        for path in self.deduplicate_paths(dropped_paths):
            if os.path.isdir(path):
                media_paths.extend(self.collect_media_files(path))
                continue
            if not os.path.isfile(path):
                continue

            _, ext = os.path.splitext(path)
            ext = ext.lower()
            if ext in self.MEDIA_EXTENSIONS:
                media_paths.append(path)
            elif ext in self.SUBTITLE_EXTENSIONS:
                subtitle_paths.append(path)

        return {
            "media_paths": self.deduplicate_paths(media_paths),
            "subtitle_paths": self.deduplicate_paths(subtitle_paths),
        }

    def deduplicate_paths(self, paths: list[str]) -> list[str]:
        unique_paths: list[str] = []
        seen: set[str] = set()
        for path in paths:
            normalized = _normalize_path(path)
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
