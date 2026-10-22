"""Normalize - per-extension dispatch (Strategy pattern, review feedback).

    md_content, err_msg = Normalize().normalize(file)

Looks up a Converter by file.extension (see converters/base.py) and
delegates. Adding a new extension means adding a new converter file and
one dispatch-table entry - never touching an existing converter.

Deliberately error-as-return-value, not the try/except+raise convention
used elsewhere in this codebase - scoped to this module and its converters
only, per explicit review request, pending a decision on whether to use
this style more broadly.
"""

from typing import Literal, Optional

from jarvis_server.internal.file_handler.reader import OpenedFile
from jarvis_server.internal.normalizer.converters.base import Converter
from jarvis_server.internal.normalizer.converters.csv import CsvNormalizer
from jarvis_server.internal.normalizer.converters.docx import DocxNormalizer
from jarvis_server.internal.normalizer.converters.html import HtmlNormalizer
from jarvis_server.internal.normalizer.converters.pdf import PDFNormalizer
from jarvis_server.internal.normalizer.converters.txt import TxtNormalizer
from jarvis_server.internal.logging.logging_config import get_logger

logger = get_logger(__name__)


class Normalize:
    def __init__(self) -> None:
        self.converters: dict[str, Converter] = {
            ".md": TxtNormalizer(),
            ".txt": TxtNormalizer(),
            ".docx": DocxNormalizer(),
            ".pdf": PDFNormalizer(),
            ".csv": CsvNormalizer(),
            ".html": HtmlNormalizer(),
        }

    def normalize(
        self, file: OpenedFile
    ) -> tuple[Optional[str], Optional[str], Optional[Literal["unsupported", "failed"]]]:
        """Returns (md_content, None, None) on success.

        On failure, the third element tells callers which of two very
        different situations this is - conflating them previously made
        real conversion failures invisible as ordinary skips (issue #115):
        "unsupported" (no converter registered for this extension - always
        expected, never an error) or "failed" (a converter was dispatched
        and its own conversion raised or otherwise came back empty)."""
        logger.debug("Normalize dispatching %s (%s)", file.path, file.extension)
        converter = self.converters.get(file.extension)
        if converter is None:
            message = f"Unsupported file extension: {file.extension}"
            logger.debug(message)
            return None, message, "unsupported"
        text, err = converter.convert(file)
        if text is None:
            return None, err, "failed"
        return text, None, None
