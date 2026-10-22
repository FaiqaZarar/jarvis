"""CsvNormalizer - .csv -> Markdown table via MarkItDown (spec §8.2, §9).

Same convert_stream-on-an-in-memory-buffer approach as DocxNormalizer /
PDFNormalizer (the file was already read by
jarvis_server.internal.file_handler.reader; MarkItDown must not touch the
filesystem itself).

No extra markitdown dependency: MarkItDown's CSV converter is built on the
standard-library `csv` module, so it works with the base package - unlike
.docx / .pdf, which need the [docx] / [pdf] extras.
"""

import io
from typing import Optional

from jarvis_server.internal.file_handler.reader import OpenedFile
from jarvis_server.internal.normalizer.converters.base import Converter
from jarvis_server.internal.logging.logging_config import get_logger

logger = get_logger(__name__)


class CsvNormalizer(Converter):
    def convert(self, file: OpenedFile) -> tuple[Optional[str], Optional[str]]:
        logger.debug("Converting %s via CsvNormalizer", file.path)
        try:
            from markitdown import MarkItDown
        except ImportError as exc:
            message = f"markitdown could not be imported; cannot convert .csv files: {exc}"
            logger.exception(message)
            return None, message

        if not file.raw_bytes:
            logger.debug("%s is empty, skipping MarkItDown", file.path)
            return "", None

        try:
            result = MarkItDown().convert_stream(
                io.BytesIO(file.raw_bytes), file_extension=file.extension
            )
            logger.debug("Converted %s: %d chars", file.path, len(result.text_content))
            return result.text_content, None
        except Exception as exc:
            logger.exception("Failed to convert %s via MarkItDown", file.path)
            return None, f"Failed to convert {file.path}: {exc}"
