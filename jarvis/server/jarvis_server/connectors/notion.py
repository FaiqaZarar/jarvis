"""Notion connector.

Implemented in J-12: markdown-export endpoint, protected-region handling
for tables/toggles, UUID canonicalization. See mvp-spec.md §8.3.
"""

from jarvis_server.internal.logging.logging_config import get_logger

logger = get_logger(__name__)
