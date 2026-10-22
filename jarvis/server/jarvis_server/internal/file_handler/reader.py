"""Generic file I/O - the file_handler.

No knowledge of file-type-specific parsing. A file is opened here and only
here: the caller (a connector) reads a file with open_file() first, then
hands the already-read OpenedFile to Normalize. Normalizer converters
never touch the filesystem themselves.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from jarvis_server.internal.logging.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class OpenedFile:
    """An already-read file, ready for a normalizer converter."""
    path: Path
    extension: str   # lowercase, including the dot, e.g. ".pdf"
    raw_bytes: bytes


def open_file(path: Path) -> tuple[Optional[OpenedFile], Optional[str]]:
    """Read a file's raw bytes.

    Returns (OpenedFile, None) on success or (None, error_message) on
    failure. Errors are a return value here, not an exception: a single
    unreadable file (most commonly a macOS TCC permission denial) is an
    expected, per-file outcome a caller branches on inline - not a
    process-level failure that should unwind the stack.
    """
    try:
        raw_bytes = path.read_bytes()
        return OpenedFile(path=path, extension=path.suffix.lower(), raw_bytes=raw_bytes), None
    except PermissionError:
        message = (
            f"Permission denied reading {path}. On macOS this is usually TCC: grant Full "
            "Disk Access to your terminal/app in System Settings > Privacy & Security > "
            "Full Disk Access, then re-run the sync."
        )
        logger.warning(message)
        return None, message
    except OSError as exc:
        message = f"Failed to read {path}: {exc}"
        logger.exception("Failed to read %s", path)
        return None, message
