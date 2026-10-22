"""DocxNormalizer - .docx -> Markdown via MarkItDown (spec §8.2, §9).

Uses convert_stream on an in-memory buffer of file.raw_bytes, not a path -
the file was already read by jarvis_server.internal.file_handler.reader, so
MarkItDown must not touch the filesystem itself.

Note for whoever adds markitdown to requirements.txt: the base package
alone cannot convert .docx - it needs the [docx] extra
(pip install 'markitdown[docx]'), or DocxNormalizer will fail at runtime
with MissingDependencyException on every real .docx file.
"""

import io
from typing import Optional

from jarvis_server.internal.file_handler.reader import OpenedFile
from jarvis_server.internal.normalizer.converters.base import Converter
from jarvis_server.internal.logging.logging_config import get_logger

logger = get_logger(__name__)


class DocxNormalizer(Converter):
    def convert(self, file: OpenedFile) -> tuple[Optional[str], Optional[str]]:
        logger.debug("Converting %s via DocxNormalizer", file.path)
        try:
            from markitdown import MarkItDown
        except ImportError as exc:
            message = f"markitdown could not be imported; cannot convert .docx files: {exc}"
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
