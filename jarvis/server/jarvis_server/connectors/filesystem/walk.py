"""Discovering which files under a watched root are eligible to ingest."""

import mimetypes
import os
from pathlib import Path
from typing import Callable, Iterator, Optional

import pathspec

from jarvis_server.internal.logging.logging_config import get_logger

logger = get_logger(__name__)

# csv/html need no extra deps beyond MarkItDown; pptx/xlsx would need
# python-pptx/openpyxl, which aren't installed.
_SUPPORTED_EXTENSIONS = {".md", ".txt", ".docx", ".pdf", ".csv", ".html"}

# Dependency/build output, never the user's own writing - and a single
# node_modules can hold thousands of READMEs that swamp the topic hierarchy.
# Pruned even without a .gitignore, since most project folders a user picks
# aren't guaranteed to have one.
_SKIPPED_DIR_NAMES = {"node_modules", "venv", "__pycache__", "site-packages", "dist", "build"}

# .docx is commonly unregistered on some platforms and .md only landed in
# CPython's mimetypes table in 3.13 - register both explicitly so mime
# detection is deterministic across machines.
for _ext, _mime in {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
    ".csv": "text/csv",
    ".html": "text/html",
}.items():
    mimetypes.add_type(_mime, _ext)


def guess_mime(file_path: Path) -> Optional[str]:
    return mimetypes.guess_type(file_path.name)[0]


def _load_gitignore_spec(root: Path) -> Optional["pathspec.PathSpec"]:
    gitignore_path = root / ".gitignore"
    if not gitignore_path.exists():
        return None
    try:
        with open(gitignore_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return pathspec.PathSpec.from_lines("gitignore", lines)
    except OSError:
        logger.exception("Failed to read .gitignore at %s", gitignore_path)
        return None


def _log_walk_error(os_error: OSError) -> None:
    # os.walk() silently drops directories it can't list by default, which
    # would make files vanish from the corpus with no trace.
    if isinstance(os_error, PermissionError):
        logger.warning(
            "Permission denied listing %s - skipping. On macOS this is usually TCC: "
            "grant Full Disk Access to your terminal/app in System Settings > "
            "Privacy & Security > Full Disk Access, then re-run the sync.",
            os_error.filename,
        )
    else:
        logger.warning("Error listing %s during walk: %s", os_error.filename, os_error)


def walk_directory(
    root: Path, on_unsupported: Optional[Callable[[str], None]] = None
) -> Iterator[Path]:
    """A directory that can't be listed (e.g. macOS TCC denial) is skipped,
    not fatal to the rest of the walk.

    `on_unsupported(extension)` fires only for the extension-mismatch skip
    branch below, not for gitignore/dotfile/directory/symlink skips - those
    are intentional exclusions, not "unsupported file type"."""
    root = Path(root).resolve()
    spec = _load_gitignore_spec(root)

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=_log_walk_error):
        current_dir = Path(dirpath)

        dirnames[:] = [
            d for d in dirnames if not d.startswith(".") and d not in _SKIPPED_DIR_NAMES
        ]

        for filename in filenames:
            if filename.startswith("."):
                continue

            file_path = current_dir / filename

            if spec is not None:
                try:
                    rel_path = file_path.relative_to(root).as_posix()
                except ValueError:
                    rel_path = filename
                if spec.match_file(rel_path):
                    continue

            if file_path.suffix.lower() not in _SUPPORTED_EXTENSIONS:
                logger.debug("Skipping unsupported file type: %s", file_path)
                if on_unsupported is not None:
                    on_unsupported(file_path.suffix.lower())
                continue

            # os.walk(followlinks=False) only stops *descending into*
            # symlinked directories - a symlinked file is still yielded
            # here, and would otherwise be read, embedded, and searchable
            # even though it's outside the folder the user actually
            # pointed Jarvis at (e.g. a synced/shared folder containing a
            # symlink to ~/.aws/credentials or ~/.ssh/known_hosts).
            if file_path.is_symlink():
                try:
                    resolved = file_path.resolve(strict=True)
                    resolved.relative_to(root)
                except (OSError, ValueError):
                    logger.warning(
                        "Skipping symlink %s - target is outside the watched root", file_path,
                    )
                    continue

            yield file_path
