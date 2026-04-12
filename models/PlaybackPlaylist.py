class PlaylistState:
    def __init__(self):
        self._paths: list[str] = []
        self._current_index = -1

    @property
    def paths(self) -> list[str]:
        return self._paths

    @property
    def current_index(self) -> int:
        return self._current_index

    def load(self, file_paths: list[str], start_index: int = 0) -> bool:
        if not file_paths:
            self._paths = []
            self._current_index = -1
            return False

        self._paths = list(file_paths)
        self._current_index = max(0, min(start_index, len(self._paths) - 1))
        return True

    def clear(self):
        self._paths = []
        self._current_index = -1

    def set_current_index(self, index: int) -> bool:
        if index < 0 or index >= len(self._paths):
            return False
        self._current_index = index
        return True

    def current_path(self) -> str | None:
        if not self._paths or self._current_index < 0 or self._current_index >= len(self._paths):
            return None
        return self._paths[self._current_index]

    def has_multiple(self) -> bool:
        return len(self._paths) > 1

    def move_previous_wrap(self) -> bool:
        if not self.has_multiple():
            return False
        self._current_index = (self._current_index - 1) % len(self._paths)
        return True

    def move_next_wrap(self) -> bool:
        if not self.has_multiple():
            return False
        self._current_index = (self._current_index + 1) % len(self._paths)
        return True

    def move_next_linear(self) -> bool:
        if self._current_index < 0:
            return False

        next_index = self._current_index + 1
        if next_index >= len(self._paths):
            return False

        self._current_index = next_index
        return True
