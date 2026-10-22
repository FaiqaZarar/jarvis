"""search MCP tool.

Implemented in J-07 (first demo milestone): hybrid BM25 + vector search
with RRF fusion, hard pre-filters, cursor pagination. See mvp-spec.md §7.1.

The shared MCPServer instance and the spec §7.1 wire models now live in
sibling modules (server.py, models.py) so hierarchy.py (J-10) registers
onto the same server and imports the same models - no later consolidation
step needed.
"""

from typing import Optional, Union

from jarvis_server.internal import db
from jarvis_server.internal.embed.embed import Embedder
from jarvis_server.internal.embed.provider import OpenRouterVoyageEmbedder
from jarvis_server.internal.logging.logging_config import get_logger
from jarvis_server.mcp_tools._common import (
    _build_chunk_hit,
    _decode_cursor,
    _encode_cursor,
    _resolve_topic_filter_ids,
)
from jarvis_server.mcp_tools.models import (
    ErrorDetail,
    ErrorResponse,
    SearchFilters,
    SearchResult,
)
from jarvis_server.mcp_tools.server import server

logger = get_logger(__name__)

DEFAULT_LIMIT = 10
MAX_LIMIT = 50
RRF_K = 60

# Sentinel vector rank for a chunk that only the FTS5 side returned, used
# only as the fused-score tiebreak sort key (issue #82) so such a chunk
# sorts after any chunk that does have a vector rank.
_TIEBREAK_UNRANKED = float("inf")

# How many top matches to pull from EACH ranked list (FTS5 keyword match
# and vector KNN) before RRF fusion. Not a documented hard limit - a
# pragmatic cap sized for MVP-scale corpora (single local user, spec's own
# scope), same "not a documented limit, adjust if a real one surfaces"
# stance as J-06's DEFAULT_BATCH_SIZE. Applied to both lists deliberately,
# not just the vector side: vec0 requires a LIMIT to function at all (a
# real OperationalError without one), which is what surfaced the need for
# a cap here in the first place - but leaving FTS5 uncapped by contrast
# would have been an accident of that mechanical constraint, not an actual
# design choice. Without a matching cap, a common keyword could scan/rank
# every matching chunk in the corpus, and the two lists feeding RRF would
# represent inconsistently-sized pools.
RANKING_POOL_SIZE = 200


# ---------------------------------------------------------------------
# Hard pre-filters -> candidate chunk_id set
# ---------------------------------------------------------------------

def _build_filter_conditions(
    conn, filters: Optional[SearchFilters]
) -> Optional[tuple[list[str], list[str], list]]:
    """Translate SearchFilters (spec §7.2) into extra JOIN clauses, WHERE
    conditions, and params to layer onto db.get_candidate_chunk_ids's
    always-on base query. Pure construction - never touches conn.execute
    itself, so the db package never has to import SearchFilters (a wire
    type from the MCP tool layer; importing it there would be a backwards
    dependency).

    Returns None when a filter is already known to match nothing (e.g. a
    named topic that doesn't exist at all) - the caller short-circuits to
    an empty candidate set rather than an error (spec §7.3)."""
    joins: list[str] = []
    conditions: list[str] = []
    params: list = []

    if filters is None:
        return joins, conditions, params

    if filters.topic is not None:
        topic_ids = _resolve_topic_filter_ids(conn, filters.topic)
        if not topic_ids:
            return None  # named topic doesn't exist at all - no matches, not an error
        joins.append("JOIN topics ON chunks.topic_id = topics.id")
        placeholders = ",".join("?" for _ in topic_ids)
        # Matches the topic itself, or any of its descendants: ancestor_path
        # (migration 0005, populated by graph_db/sync.py) is a JSON array of
        # ancestor topic ids, root-to-self - a topic is a descendant of X if
        # X appears in its ancestor_path.
        conditions.append(
            f"""(topics.id IN ({placeholders})
                 OR EXISTS (
                     SELECT 1 FROM json_each(topics.ancestor_path) je
                     WHERE je.value IN ({placeholders})
                 ))"""
        )
        params.extend(topic_ids)
        params.extend(topic_ids)

    if filters.source is not None:
        conditions.append("document_sources.connector = ?")
        params.append(filters.source)

    if filters.date_range is not None:
        if filters.date_range.from_date is not None:
            conditions.append("document_sources.source_modified_at >= ?")
            params.append(filters.date_range.from_date)
        if filters.date_range.to_date is not None:
            conditions.append("document_sources.source_modified_at <= ?")
            params.append(filters.date_range.to_date)

    if filters.file_type is not None:
        conditions.append("document_sources.mime = ?")
        params.append(filters.file_type)

    return joins, conditions, params


def _candidate_chunk_ids(conn, filters: Optional[SearchFilters]) -> set[str]:
    """The search candidate set: the always-on tombstone exclusion, plus
    whatever hard pre-filters (spec §7.2) are present. Always returns a
    real set (never None); an empty set flows through to empty results
    downstream, not an error (spec §7.3)."""
    built = _build_filter_conditions(conn, filters)
    if built is None:
        return set()
    joins, conditions, params = built
    return db.get_candidate_chunk_ids(conn, joins, conditions, params)


# ---------------------------------------------------------------------
# FTS5 + vector ranking
# ---------------------------------------------------------------------

# A small English stop-word set for FTS query construction (issue #52).
# The keyword side ORs its terms, so without this a question like
# "how smart is an octopus and why" would rank the FTS list on matches
# for "is"/"an"/"and" - which appear in nearly every chunk - and that
# noisy list then poisons RRF fusion. Deliberately short: only common
# function words with no retrieval value. Domain nouns are never here,
# and terms under _FTS_MIN_TERM_LEN are dropped anyway, so most 1-2 char
# stop words ("a", "an", "is", "of", "to", "in", "or", ...) don't need
# listing.
_FTS_STOPWORDS = frozenset({
    "the", "and", "for", "are", "was", "were", "been", "being", "you",
    "your", "yours", "our", "ours", "its", "his", "her", "hers", "him",
    "she", "they", "them", "their", "theirs", "this", "that", "these",
    "those", "with", "from", "have", "has", "had", "having", "not", "but",
    "all", "any", "some", "than", "then", "there", "here", "into", "onto",
    "out", "about", "over", "under", "such", "each", "very", "just",
    "more", "most", "also", "how", "why", "what", "when", "where", "which",
    "who", "whom", "whose", "does", "did", "doing", "can", "could", "will",
    "would", "shall", "should", "may", "might", "must", "get", "got",
    "gets", "getting",
})

_FTS_MIN_TERM_LEN = 3


def _sanitize_fts5_query(query_text: str) -> str:
    """Turn an arbitrary natural-language string into a safe FTS5 MATCH
    expression. FTS5 parses the right-hand side of MATCH as its own query
    grammar, so punctuation a user would never think twice about - a
    hyphen in "write-ahead logging", a stray quote - is read as an
    operator and raises OperationalError (e.g. `no such column: ahead`),
    which then surfaces to the caller as a bogus invalid_argument error
    instead of results.

    Strategy:

      1. Split on whitespace, strip embedded double quotes (a lone `"`
         inside a quoted phrase is itself a syntax error).
      2. Drop terms shorter than `_FTS_MIN_TERM_LEN` and terms in
         `_FTS_STOPWORDS` - common function words carry no retrieval
         signal and, under the OR join below, would let near-universal
         words dominate the keyword ranking (issue #52).
      3. Wrap each surviving term in double quotes so FTS5 reads it as a
         literal phrase token, not syntax.
      4. Join with `OR`, not the implicit `AND`: a chunk matching *any*
         content word is a candidate, and BM25 (`ORDER BY rank`) then
         surfaces the ones matching more of them, and rarer ones, first.
         Requiring every word meant a full-sentence question matched
         nothing on the keyword side and search fell through to
         vector-only.

    If nothing survives (all-stopword / all-short query), returns an empty
    string; the caller short-circuits and the result is a clean
    vector-only ranking rather than a stop-word-poisoned keyword list.

    The FTS5 index itself is Porter-stemmed (migration 0009), so
    "checkpoint" also matches "checkpoints" etc. Vector-side ranking is
    untouched - this is purely an FTS5 concern."""
    terms = []
    for raw_term in query_text.split():
        cleaned = raw_term.replace('"', "")
        if len(cleaned) < _FTS_MIN_TERM_LEN:
            continue
        if cleaned.lower() in _FTS_STOPWORDS:
            continue
        terms.append(f'"{cleaned}"')
    return " OR ".join(terms)


def _fts5_ranked_ids(
    conn, query_text: str, candidate_ids: Optional[set[str]], top_k: int
) -> dict[str, int]:
    """chunk_id -> 1-based rank position (1 = best keyword match). Scoped
    to candidate_ids so the hard pre-filter is applied before ranking, not
    after - not just a Python-side post-filter, which would corrupt the
    rank numbers RRF depends on. Capped at top_k for consistency with the
    vector side's pool (see RANKING_POOL_SIZE) - FTS5's MATCH doesn't
    require a LIMIT to function, unlike vec0, but leaving this list
    unbounded while the vector list is capped would make the two ranked
    pools feeding RRF inconsistently sized, not a deliberate asymmetry.

    The raw query is pre-tokenized into an OR of literal FTS5 phrase
    tokens (see _sanitize_fts5_query) so ordinary punctuation can't be
    misparsed as FTS5 query operators and crash the whole search, and a
    partial keyword overlap still produces a ranked list."""
    if candidate_ids is not None and not candidate_ids:
        return {}

    match_expr = _sanitize_fts5_query(query_text)
    if not match_expr:
        return {}

    return db.get_fts5_ranked_chunk_ids(conn, match_expr, candidate_ids, top_k)


def _vector_ranked_ids(
    conn, query_vector: list[float], candidate_ids: Optional[set[str]], top_k: int
) -> dict[str, int]:
    """chunk_id -> 1-based rank position (1 = nearest). Just the
    empty-candidate-set short-circuit; the actual two-path ranking query
    (native vec0 KNN when unfiltered, Python-side scoring when filtered -
    the second path exists to sidestep a confirmed vec0 quirk) lives in
    db.embedding.get_vector_ranked_chunk_ids, moved there verbatim
    (comment included) when this module's SQL was centralized into the
    db package - see that function's docstring for the full rationale."""
    if candidate_ids is not None and not candidate_ids:
        return {}

    return db.get_vector_ranked_chunk_ids(conn, query_vector, candidate_ids, top_k)


def _rrf_score(fts_rank: Optional[int], vector_rank: Optional[int]) -> float:
    """Reciprocal Rank Fusion, k=60. A chunk missing from one ranked list
    contributes 0 for that list's term (standard RRF behavior, not a
    penalty). Normalized by the theoretical max (rank 1 in both lists) to
    land in [0, 1] - locked design, confirmed before this build."""
    raw = 0.0
    if fts_rank is not None:
        raw += 1.0 / (RRF_K + fts_rank)
    if vector_rank is not None:
        raw += 1.0 / (RRF_K + vector_rank)
    max_possible = 2.0 / (RRF_K + 1)
    return raw / max_possible


# ---------------------------------------------------------------------
# search() - internal implementation (testable, DI'd) + registered tool
# ---------------------------------------------------------------------

def _search_impl(
    conn,
    embedder: Embedder,
    query: str,
    filters: Optional[SearchFilters],
    limit: int,
    cursor: Optional[str],
) -> Union[SearchResult, ErrorResponse]:
    logger.info(
        "search: query=%r limit=%d cursor_present=%s filters=%s",
        query, limit, cursor is not None, filters,
    )

    current_sync_id = db.get_current_sync_id(conn)

    offset = 0
    if cursor is not None:
        try:
            decoded = _decode_cursor(cursor)
        except Exception:
            logger.warning("search: malformed cursor")
            return ErrorResponse(error=ErrorDetail(
                code="invalid_argument", message="cursor could not be decoded",
            ))
        if decoded["sync_id"] != current_sync_id:
            logger.info(
                "search: stale cursor - cursor sync_id=%s current_sync_id=%s",
                decoded["sync_id"], current_sync_id,
            )
            return ErrorResponse(error=ErrorDetail(
                code="stale_cursor", message="cursor is from a prior sync; restart pagination",
            ))
        offset = decoded["offset"]

    limit = min(limit, MAX_LIMIT)

    # _candidate_chunk_ids always returns a real set now (tombstone
    # exclusion is unconditional), so an empty result here means "nothing
    # live matches" - filters or not. Consequence: a fully-unfiltered
    # search no longer hits vec0's native-KNN fast path in
    # _vector_ranked_ids (that path needs candidate_ids is None); every
    # search now ranks against an explicit candidate set. Intentional
    # correctness-over-speed trade-off, fine at MVP scale.
    candidate_ids = _candidate_chunk_ids(conn, filters)
    if not candidate_ids:
        logger.info("search: candidate set is empty (no live chunks match)")
        return SearchResult(query=query, results=[], next_cursor=None)

    fts_ranks = _fts5_ranked_ids(conn, query, candidate_ids, RANKING_POOL_SIZE)

    query_vector = embedder.embed([query])[0]
    vector_ranks = _vector_ranked_ids(conn, query_vector, candidate_ids, RANKING_POOL_SIZE)

    logger.info(
        "search: fts_matches=%d vector_matches=%d", len(fts_ranks), len(vector_ranks),
    )

    all_chunk_ids = set(fts_ranks) | set(vector_ranks)
    scored = [
        (chunk_id, _rrf_score(fts_ranks.get(chunk_id), vector_ranks.get(chunk_id)))
        for chunk_id in all_chunk_ids
    ]
    # Deterministic tiebreak (issue #82): _rrf_score is symmetric, so a
    # chunk at (bm25 rank 2, vector rank 1) and one at (bm25 rank 1,
    # vector rank 2) score identically. Sorting on score alone left the
    # winner decided by set-iteration order over chunk_ids - i.e. by
    # PYTHONHASHSEED - so two identical queries against an unchanged index
    # could return different top hits. Break score ties by vector rank
    # ascending (a BM25 tie is likelier a spurious single-term match; see
    # docs/J14_ACCURACY_EVAL.md), then by chunk_id so the order is fully
    # determined in every case. A chunk absent from the vector list sorts
    # after any chunk that has a vector rank.
    scored.sort(
        key=lambda pair: (
            -pair[1],
            vector_ranks.get(pair[0], _TIEBREAK_UNRANKED),
            pair[0],
        )
    )

    page = scored[offset:offset + limit]
    has_more = len(scored) > offset + limit

    results = [_build_chunk_hit(conn, chunk_id, score) for chunk_id, score in page]

    next_cursor = _encode_cursor(current_sync_id, offset + limit) if has_more else None

    logger.info(
        "search: total_ranked=%d returned=%d has_more=%s", len(scored), len(results), has_more,
    )

    return SearchResult(query=query, results=results, next_cursor=next_cursor)


@server.tool()
def search(
    query: str,
    filters: Optional[SearchFilters] = None,
    limit: int = DEFAULT_LIMIT,
    cursor: Optional[str] = None,
) -> Union[SearchResult, ErrorResponse]:
    """Search the user's personal indexed document corpus (local files,
    Drive, Notion). Use this whenever the user asks about their own notes,
    files, or previously saved content. Hybrid BM25 + vector search with
    RRF fusion, hard pre-filters, and cursor pagination. Read-only.
    See mvp-spec.md §7.1-7.3."""
    conn = db.get_connection()
    try:
        return _search_impl(conn, OpenRouterVoyageEmbedder(), query, filters, limit, cursor)
    except RuntimeError:
        # OpenRouterVoyageEmbedder._embed_batch's own auth-failure path
        # (HTTP 401/403 from the embedding provider) raises exactly this,
        # and nothing else reachable from _search_impl does (confirmed by
        # grepping every `raise RuntimeError` in jarvis_server - the only
        # other ones are in unrelated modules never called from search).
        # This was previously mislabeled "invalid_argument", the same
        # code used for a genuinely malformed query - misleading, since a
        # missing/wrong/expired EMBEDDING_API_KEY is a service-
        # configuration problem, not anything about what the caller sent.
        # "internal_error" is this project's own established code for
        # exactly that distinction (already used identically in
        # sql_query.py) - the real MCP SDK's own closest equivalent,
        # checked directly rather than guessed, is mcp.types.INTERNAL_ERROR
        # (-32603, JSON-RPC's "the server encountered an unexpected
        # condition"), which ErrorDetail.code (a plain str field, not tied
        # to MCP's own integer wire codes) can't hold directly - "internal_error"
        # is this project's existing string analog of that same concept.
        logger.exception("search() failed unexpectedly for query=%r", query)
        return ErrorResponse(error=ErrorDetail(
            code="internal_error", message="search failed due to an internal error",
        ))
    except Exception:
        # Every other unexpected failure - unrelated to the auth-failure
        # case above, and deliberately left unchanged: still
        # "invalid_argument", exactly as it was before this fix, since
        # nothing here was asked to change and doing so wasn't checked
        # against whatever currently depends on this specific code/path.
        logger.exception("search() failed unexpectedly for query=%r", query)
        return ErrorResponse(error=ErrorDetail(
            code="invalid_argument", message="search failed due to an internal error",
        ))
    finally:
        conn.close()
