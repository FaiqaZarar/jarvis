"""Embedding interface + cache.

Implemented in J-06: swappable embedder interface, embedding cache keyed by
chunk_hash, BM25 (FTS5) + vector index. See mvp-spec.md §5.1, §9.

Embedder is deliberately a thin abstract interface (embed(texts) -> vectors
only) so the concrete provider (OpenRouterVoyageEmbedder, in provider.py)
can be swapped without touching embed_and_index_chunks or anything
downstream of it.
"""

import sqlite3
import struct
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Callable, Optional

from jarvis_server.internal import db
from jarvis_server.internal.logging.logging_config import get_logger

logger = get_logger(__name__)

EMBEDDING_DIMENSIONS = 1024

# Fallback for any Embedder that doesn't set its own batch_size (e.g. a
# test stub) - not tied to a specific provider, so this stays here rather
# than in provider.py.
DEFAULT_BATCH_SIZE = 100


class IngestCancelled(Exception):
    """A cooperative cancel request (`on_cancel_requested`) landed at a
    checkpoint - embed's own between-batches check, or pipeline.py's
    between-phases one. `partial` is whatever stats the caller had
    gathered up to that point, for a caller that wants to report them."""

    def __init__(self, partial: dict):
        self.partial = partial
        super().__init__("ingest cancelled")


class Embedder(ABC):
    """The interface every embedding provider implements."""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts, returning one vector per input text, in
        the same order."""
        raise NotImplementedError


def _vector_to_blob(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def embed_and_index_chunks(
    sqlite_conn: sqlite3.Connection,
    embedder: Embedder,
    on_batch: Optional[Callable[[int, int, int, int], None]] = None,
    *,
    run_id: Optional[str] = None,
    phase: str = "embed",
    on_cancel_requested: Optional[Callable[[], bool]] = None,
) -> dict:
    """Embed every chunk that isn't already in chunk_vectors, and keep
    chunks_fts in sync for the same chunks in the same pass.

    A chunk_id already present in chunk_vectors is assumed unchanged and
    is never re-embedded, so re-running this on the same data makes zero
    new API calls (J-06's stated acceptance criterion).

    On a run that actually embeds something new, this also bumps
    sync_state.current_sync_id - search() reads that value to stamp and
    validate pagination cursors (spec §7.3's stale_cursor requirement). A
    pure cache-hit run (nothing new) does not bump it.

    `on_batch`, if given, is called as `on_batch(current_batch,
    total_batches, processed_items, total_items)` once before the loop
    starts (current_batch=0) and again after each batch completes - this
    module has no idea who's listening or where that goes (same seam as
    `pipeline.py`'s `on_phase`). Not called at all when there's nothing
    pending to embed (a pure cache-hit run), and not called for a batch
    that raises.

    `run_id`/`phase` identify this call's row in `ingest_run_phases`
    (migration 0016, replacing migration 0015's single-row
    `ingest_progress`) - `run_id=None` (a caller with no run to attribute
    this to, e.g. a direct test) skips all DB progress writes entirely,
    still calling `on_batch` normally. Each batch commits its progress
    row on its own rather than the whole run committing once at the end -
    a reader on a separate connection (`GET /ingest/status`, `query_db`)
    then sees real, live-updating counts instead of nothing until the
    very end. The trade-off: a batch that raises only rolls back that
    batch, not prior ones - whatever embedded before the failure stays
    embedded (chunk_id membership in chunk_vectors is exactly how the
    cache-check above decides what's already done, so a retry resumes
    correctly). The phase row's own `status` is what tells a reader
    "stopped early, did not finish" apart from a plausible-looking count.

    `on_cancel_requested`, if given, is checked between batches (never
    mid-batch): a cooperative checkpoint, same granularity as
    `pipeline.run_ingest`'s own between-phase checks. Raises
    `IngestCancelled` with the stats gathered so far when it returns True.

    Returns a stats dict: {total_chunks, cache_hits, newly_embedded, api_calls_made}.
    """
    stats = {"total_chunks": 0, "cache_hits": 0, "newly_embedded": 0, "api_calls_made": 0}
    all_chunks = db.get_all_chunk_texts(sqlite_conn)
    stats["total_chunks"] = len(all_chunks)

    already_embedded = db.get_embedded_chunk_ids(sqlite_conn)
    pending = [row for row in all_chunks if row["chunk_id"] not in already_embedded]
    stats["cache_hits"] = len(all_chunks) - len(pending)

    if not pending:
        logger.info(
            "embed_and_index_chunks: total=%d cache_hits=%d newly_embedded=0 api_calls=0",
            stats["total_chunks"], stats["cache_hits"],
        )
        return stats

    batch_size = getattr(embedder, "batch_size", DEFAULT_BATCH_SIZE)
    total_batches = -(-len(pending) // batch_size)  # ceil div
    total_items = len(pending)

    if run_id is not None:
        db.set_ingest_run_phase_progress(
            sqlite_conn, run_id, phase, total_items, 0, total_batches, 0,
            datetime.now(timezone.utc).isoformat(),
        )
    if on_batch is not None:
        on_batch(0, total_batches, 0, total_items)

    for batch_num, start in enumerate(range(0, len(pending), batch_size), start=1):
        batch = pending[start:start + batch_size]
        texts = [row["text"] for row in batch]
        processed_before_batch = stats["newly_embedded"]

        try:
            vectors = embedder.embed(texts)
            stats["api_calls_made"] += 1

            for row, vector in zip(batch, vectors):
                if len(vector) != EMBEDDING_DIMENSIONS:
                    # chunk_vectors' schema is FLOAT[1024], fixed at
                    # migration time (0006) - a model swap that changes
                    # output dimension can't be allowed to flow through
                    # silently, or it corrupts the vector index. Fail
                    # loudly here, before anything is written.
                    raise ValueError(
                        f"embedding dimension mismatch: model returned {len(vector)}, "
                        f"table expects {EMBEDDING_DIMENSIONS}, changing models requires "
                        "a new migration and a full re-embed"
                    )

                db.insert_chunk_vector(
                    sqlite_conn, row["chunk_id"], _vector_to_blob(vector), commit=False
                )
                db.insert_chunk_fts(sqlite_conn, row["chunk_id"], row["text"], commit=False)
                stats["newly_embedded"] += 1

            if run_id is not None:
                db.set_ingest_run_phase_progress(
                    sqlite_conn, run_id, phase, total_items, stats["newly_embedded"],
                    total_batches, batch_num, datetime.now(timezone.utc).isoformat(), commit=False,
                )
            sqlite_conn.commit()
        except Exception:
            sqlite_conn.rollback()
            # This batch's rows never survived the rollback, however many
            # of them stats["newly_embedded"] counted before the raise -
            # reset it to what was actually committed (the prior batch).
            stats["newly_embedded"] = processed_before_batch
            if run_id is not None:
                db.set_ingest_run_phase_progress(
                    sqlite_conn, run_id, phase, total_items, processed_before_batch,
                    total_batches, batch_num - 1, datetime.now(timezone.utc).isoformat(),
                )
            logger.exception(
                "embed_and_index_chunks: batch %d/%d failed", batch_num, total_batches
            )
            raise

        if on_batch is not None:
            on_batch(batch_num, total_batches, stats["newly_embedded"], total_items)

        if batch_num < total_batches and on_cancel_requested is not None and on_cancel_requested():
            logger.info("embed_and_index_chunks: cancelled after batch %d/%d", batch_num, total_batches)
            raise IngestCancelled(dict(stats))

    if stats["newly_embedded"] > 0:
        # Not folded into a batch's own commit above - this only needs to
        # happen once, after every batch that's going to run already has
        # (a pure cache-hit run leaves search-relevant state untouched,
        # so existing cursors should stay valid rather than being
        # needlessly invalidated).
        db.bump_sync_state(sqlite_conn, str(uuid.uuid4()))

    logger.info(
        "embed_and_index_chunks: total=%d cache_hits=%d newly_embedded=%d api_calls=%d",
        stats["total_chunks"], stats["cache_hits"],
        stats["newly_embedded"], stats["api_calls_made"],
    )
    return stats
