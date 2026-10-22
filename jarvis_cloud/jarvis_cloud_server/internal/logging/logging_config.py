"""Shared logging setup.

Every module that touches external calls, I/O, or the database should get
its logger from here rather than rolling its own, so log format/level
stays consistent across the whole service.

Usage:
    from jarvis_cloud_server.internal.logging.logging_config import get_logger
    logger = get_logger(__name__)
"""

import logging
import os

_CONFIGURED = False


def _configure() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    level = os.environ.get("JARVIS_CLOUD_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    _configure()
    return logging.getLogger(name)
