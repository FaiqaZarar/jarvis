"""TxtNormalizer - .md and .txt pass-through via robust text decoding.

UTF-8 -> charset-normalizer fallback -> strip BOM. Must run before
jarvis_server.internal.normalizer.hashing's normalizer, so local files hash
identically to the same content pulled in from other sources.
"""

from typing import Optional

from charset_normalizer import from_bytes

from jarvis_server.internal.file_handler.reader import OpenedFile
from jarvis_server.internal.normalizer.converters.base import Converter
from jarvis_server.internal.logging.logging_config import get_logger

logger = get_logger(__name__)

_BOM = "﻿"


class TxtNormalizer(Converter):
    def convert(self, file: OpenedFile) -> tuple[Optional[str], Optional[str]]:
        logger.debug("Converting %s via TxtNormalizer", file.path)
        try:
            text = file.raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            logger.warning(
                "UTF-8 decode failed for %s, falling back to charset-normalizer", file.path
            )
            result = from_bytes(file.raw_bytes).best()
            if result is None:
                message = f"Could not detect a text encoding for {file.path}"
                logger.error(message)
                return None, message
            text = str(result)
        logger.debug("Converted %s: %d chars", file.path, len(text))
        return text.lstrip(_BOM), None
