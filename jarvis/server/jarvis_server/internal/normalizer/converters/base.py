"""The interface every per-extension converter implements (Strategy pattern).

Design goal (review feedback): adding a new extension later means adding
one new file here and registering it in Normalize's dispatch table - never
editing an existing converter.
"""

from abc import ABC, abstractmethod
from typing import Optional

from jarvis_server.internal.file_handler.reader import OpenedFile


class Converter(ABC):
    @abstractmethod
    def convert(self, file: OpenedFile) -> tuple[Optional[str], Optional[str]]:
        """Convert an already-read file to Markdown text.

        Returns (markdown_text, None) on success or (None, error_message)
        on failure - error-as-return-value, matching Normalize.normalize's
        contract (see jarvis/internal/normalizer/normalize.py).
        """
        raise NotImplementedError
