"""PDFNormalizer - clean PDFs -> Markdown via MarkItDown (spec §8.2, §9).

Same convert_stream approach as DocxNormalizer - kept as its own class
(not shared with Docx) since PDF handling is expected to diverge later
(spec §9 flags gating scanned PDFs via a PyMuPDF text-layer check as a
likely future addition here, out of scope for the current tickets).

Note: the base markitdown package needs its [pdf] extra
(pip install 'markitdown[pdf]') to actually convert PDFs at runtime.
"""

import io
from typing import Optional

from jarvis_server.internal.file_handler.reader import OpenedFile
from jarvis_server.internal.normalizer.converters.base import Converter
from jarvis_server.internal.logging.logging_config import get_logger

logger = get_logger(__name__)


class PDFNormalizer(Converter):
    def convert(self, file: OpenedFile) -> tuple[Optional[str], Optional[str]]:
        logger.debug("Converting %s via PDFNormalizer", file.path)
        try:
            from markitdown import MarkItDown
        except ImportError as exc:
            message = f"markitdown could not be imported; cannot convert .pdf files: {exc}"
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
