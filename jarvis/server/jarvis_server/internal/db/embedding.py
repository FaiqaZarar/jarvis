"""chunk_vectors / chunks_fts / sync_state (embedding + indexing, J-06).

These three tables are only ever written together, in one atomic commit,
by embed_and_index_chunks - see database-access.md for why they share one
module instead of three."""

import sqlite3
import struct
from typing import Optional

from jarvis_server.internal.db._middleware import read_query, write_query


@read_query
def get_all_chunk_texts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """(chunk_id, text) for every chunk, regardless of embedding status."""
    return conn.execute("SELECT chunk_id, text FROM chunks").fetchall()


@read_query
def get_embedded_chunk_ids(conn: sqlite3.Connection) -> set[str]:
    return {
        row["chunk_id"] for row in conn.execute("SELECT chunk_id FROM chunk_vectors").fetchall()
    }


@read_query
def get_all_chunk_vectors(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """(chunk_id, embedding) for every embedded chunk, in a stable
    chunk_id order - the organize engine's clustering input."""
    return conn.execute(
        "SELECT chunk_id, embedding FROM chunk_vectors ORDER BY chunk_id"
    ).fetchall()


@read_query
def get_chunk_ids_sample(conn: sqlite3.Connection, limit: int) -> set[str]:
    """Up to `limit` chunk ids, in a stable order - the debug UI's
    playground uses this to restrict a trace run to a manageable sample
    instead of the whole corpus."""
    rows = conn.execute(
        "SELECT chunk_id FROM chunk_vectors ORDER BY chunk_id LIMIT ?", (limit,)
    ).fetchall()
    return {row["chunk_id"] for row in rows}


@read_query
def count_chunk_vectors(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0]


@write_query
def insert_chunk_vector(conn: sqlite3.Connection, chunk_id: str, embedding: bytes) -> None:
    """Write one embedding. Callers batching several writes into one
    commit (embed_and_index_chunks: this, insert_chunk_fts, and
    bump_sync_state) should pass commit=False and commit once at the
    end, so a batch failure partway through never leaves the index
    half-written."""
    conn.execute(
        "INSERT INTO chunk_vectors (chunk_id, embedding) VALUES (?, ?)",
        (chunk_id, embedding),
    )


@write_query
def insert_chunk_fts(conn: sqlite3.Connection, chunk_id: str, text: str) -> None:
    """Write one chunks_fts row. See insert_chunk_vector on batching."""
    conn.execute(
        "INSERT INTO chunks_fts (chunk_id, text) VALUES (?, ?)",
        (chunk_id, text),
    )


@write_query
def bump_sync_state(conn: sqlite3.Connection, sync_id: str) -> None:
    """Stamp a new current_sync_id (single-row table, id=1), invalidating
    outstanding search cursors (spec §7.3). Commits immediately by
    default: pipeline's organize/reconcile setup wants the id to stick
    even if organize/reconcile fails afterward. embed_and_index_chunks
    passes commit=False to fold the bump into its own atomic commit
    alongside the vectors/fts rows it just wrote."""
    conn.execute(
        "INSERT INTO sync_state (id, current_sync_id) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET current_sync_id = excluded.current_sync_id",
        (sync_id,),
    )


@read_query
def get_current_sync_id(conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute("SELECT current_sync_id FROM sync_state WHERE id = 1").fetchone()
    return row["current_sync_id"] if row else None


@read_query
def get_chunk_vector(conn: sqlite3.Connection, chunk_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT embedding FROM chunk_vectors WHERE chunk_id = ?", (chunk_id,)
    ).fetchone()


@read_query
def get_fts5_ranked_chunk_ids(
    conn: sqlite3.Connection,
    match_expr: str,
    candidate_ids: Optional[set[str]],
    top_k: int,
) -> dict[str, int]:
    """chunk_id -> 1-based rank position (1 = best keyword match), for an
    already-sanitized FTS5 MATCH expression (see search.py's
    _sanitize_fts5_query - this function never sees raw query text).
    Scoped to candidate_ids so the hard pre-filter is applied before
    ranking, not after - not just a Python-side post-filter, which would
    corrupt the rank numbers RRF depends on. Capped at top_k for
    consistency with the vector side's pool - FTS5's MATCH doesn't
    require a LIMIT to function, unlike vec0, but leaving this list
    unbounded while the vector list is capped would make the two ranked
    pools feeding RRF inconsistently sized, not a deliberate asymmetry."""
    sql = "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH ?"
    params: list = [match_expr]
    if candidate_ids is not None:
        placeholders = ",".join("?" for _ in candidate_ids)
        sql += f" AND chunk_id IN ({placeholders})"
        params.extend(candidate_ids)
    sql += " ORDER BY rank LIMIT ?"
    params.append(top_k)

    rows = conn.execute(sql, params).fetchall()
    return {row["chunk_id"]: i + 1 for i, row in enumerate(rows)}


def _squared_euclidean(a: list[float], b: list[float]) -> float:
    """Ranking-only distance (no sqrt - monotonic with true Euclidean, so
    ordering is identical, and RRF only needs rank position, not the raw
    value)."""
    return sum((x - y) ** 2 for x, y in zip(a, b))


@read_query
def get_vector_ranked_chunk_ids(
    conn: sqlite3.Connection,
    query_vector: list[float],
    candidate_ids: Optional[set[str]],
    top_k: int,
) -> dict[str, int]:
    """chunk_id -> 1-based rank position (1 = nearest).

    Two paths, deliberately not one:

    - No candidate filter: use vec0's own KNN operator directly
      (`MATCH ... ORDER BY distance LIMIT`) - efficient for a
      potentially-large unfiltered corpus.
    - A candidate filter is present: fetch exactly those chunks' stored
      vectors (a plain, non-KNN lookup - no `MATCH` at all) and rank them
      in Python instead.

    The second path exists because of a real, confirmed vec0 quirk
    (try_vec0_singleton_filter_probe.py): combining `embedding MATCH ?`
    with an additional `WHERE` restriction that resolves to exactly one
    row raises `OperationalError: A LIMIT or 'k = ?' constraint is
    required on vec0 knn queries` - SQLite's planner rewrites a
    single-value `IN (?)`/`= ?` against chunk_id (vec0's PRIMARY KEY) into
    a point lookup, which never reaches vec0's KNN code path at all, so it
    never sees the LIMIT that path requires. A subquery-wrapped
    workaround exists (get vec0's unfiltered top-K first, filter after)
    but reintroduces exactly the under-return risk already ruled out
    earlier (try_vec0_filter_probe.py) - a real filtered-in candidate
    ranked outside that initial top-K window would be silently dropped.
    Computing distance directly against the (already small, already
    hard-filtered) candidate set sidesteps the quirk entirely and cannot
    under-return by construction - it isn't a KNN scan at all."""
    if candidate_ids is None:
        blob = struct.pack(f"{len(query_vector)}f", *query_vector)
        rows = conn.execute(
            "SELECT chunk_id FROM chunk_vectors WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (blob, top_k),
        ).fetchall()
        return {row["chunk_id"]: i + 1 for i, row in enumerate(rows)}

    placeholders = ",".join("?" for _ in candidate_ids)
    rows = conn.execute(
        f"SELECT chunk_id, embedding FROM chunk_vectors WHERE chunk_id IN ({placeholders})",
        list(candidate_ids),
    ).fetchall()

    scored = [
        (row["chunk_id"], _squared_euclidean(query_vector, struct.unpack(f"{len(query_vector)}f", row["embedding"])))
        for row in rows
    ]
    scored.sort(key=lambda pair: pair[1])
    return {chunk_id: i + 1 for i, (chunk_id, _distance) in enumerate(scored)}
