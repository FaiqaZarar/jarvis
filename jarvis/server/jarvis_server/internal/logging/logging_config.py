"""Shared logging setup.

Every module that touches external calls, I/O, DB, or webhooks should get
its logger from here rather than rolling its own, so log format/level stays
consistent across the whole pipeline.

Usage:
    from jarvis_server.internal.logging.logging_config import get_logger
    logger = get_logger(__name__)
"""

import logging
import os
import sys
from pathlib import Path

_CONFIGURED = False

_MAX_LOG_BYTES = 5 * 1024 * 1024
_LOG_BACKUPS = 3


def _configure() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    level = os.environ.get("JARVIS_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    _configure()
    return logging.getLogger(name)


def default_log_dir() -> Path:
    """The platform's conventional per-user log folder, created if missing:
    `%LOCALAPPDATA%\\Jarvis\\Logs` on Windows, `~/Library/Logs/Jarvis` on
    macOS, `$XDG_STATE_HOME/jarvis` (or `~/.local/state/jarvis`) elsewhere."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        path = Path(base) / "Jarvis" / "Logs" if base else Path.home() / "AppData" / "Local" / "Jarvis" / "Logs"
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Logs" / "Jarvis"
    else:
        base = os.environ.get("XDG_STATE_HOME")
        path = (Path(base) if base else Path.home() / ".local" / "state") / "jarvis"
    path.mkdir(parents=True, exist_ok=True)
    return path


def rotate_if_large(path: Path, max_bytes: int = _MAX_LOG_BYTES, backups: int = _LOG_BACKUPS) -> None:
    """Rotate `path` to `path.1` (shifting older backups up, dropping the
    oldest) once it passes `max_bytes`. Done at startup rather than with a
    RotatingFileHandler because several processes append to the same log
    file, and a handler can't rename a file another process holds open.
    The same applies here on Windows, so a failed rename is skipped and
    retried on the next start."""
    try:
        if path.stat().st_size < max_bytes:
            return
    except OSError:
        return
    try:
        for n in range(backups - 1, 0, -1):
            older = path.with_name(f"{path.name}.{n}")
            if older.exists():
                os.replace(older, path.with_name(f"{path.name}.{n + 1}"))
        os.replace(path, path.with_name(f"{path.name}.1"))
    except OSError:
        pass
