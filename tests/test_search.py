"""J-07 acceptance check: search()'s RRF fusion, hard pre-filters, cursor
pagination/staleness, and limit capping. No network calls - uses a stub
query embedder throughout. See tests/test_search_live.py for the real-API
test."""

import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis_server.internal import db
from jarvis_server.internal.embed.embed import Embedder, _vector_to_blob
from jarvis_server.internal.migrate import run_migrations
from jarvis_server.mcp_tools.models import (
    DateRange,
    ErrorResponse,
    SearchFilters,
    SearchResult,
)
from jarvis_server.mcp_tools import search as search_mod
from jarvis_server.mcp_tools.search import (
    _decode_cursor,
    _encode_cursor,
    _fts5_ranked_ids,
    _sanitize_fts5_query,
    _search_impl,
    search,
)


class StubQueryEmbedder(Embedder):
    """Always returns the same fixed vector for the query text, regardless
    of what's passed in - lets tests control vector-KNN ranking precisely
    without a real network call."""

    def __init__(self, vector: list[float]):
        self._vector = vector

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector for _ in texts]


def _vec(first_value: float) -> list[float]:
    return [first_value] + [0.0] * 1023


@pytest.fixture
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    connection = db.get_connection(db_path)
    yield connection
    connection.close()


def _seed_source(conn, content_hash, connector="filesystem", source_id=None,
                  title="doc.md", mime=None, source_modified_at="2026-01-01T00:00:00Z"):
    source_id = source_id or f"/tmp/{content_hash}"
    conn.execute(
        "INSERT INTO documents (content_hash, normalized_markdown, first_seen_at, last_ingested_at) "
        "VALUES (?, '# T', 'now', 'now') ON CONFLICT DO NOTHING",
        (content_hash,),
    )
    db.insert_document_source(
        conn, source_id=source_id, connector=connector, content_hash=content_hash,
        source_last_seen="now", source_modified_at=source_modified_at, mime=mime, title=title,
    )


def _seed_chunk(conn, chunk_id, content_hash, text):
    conn.execute(
        "INSERT INTO chunks (chunk_id, content_hash, ordinal, chunk_hash, text) VALUES (?, ?, 0, ?, ?)",
        (chunk_id, content_hash, chunk_id, text),
    )


def _index_fts(conn, chunk_id, text):
    conn.execute("INSERT INTO chunks_fts (chunk_id, text) VALUES (?, ?)", (chunk_id, text))


def _index_vector(conn, chunk_id, vector):
    conn.execute(
        "INSERT INTO chunk_vectors (chunk_id, embedding) VALUES (?, ?)",
        (chunk_id, _vector_to_blob(vector)),
    )


def _tombstone_sources(conn, content_hash):
    conn.execute(
        "UPDATE document_sources SET tombstoned = 1 WHERE content_hash = ?", (content_hash,)
    )


def _seed_topic(conn, topic_id, name, ancestor_path=None):
    conn.execute(
        "INSERT INTO topics (id, name, level, ancestor_path) VALUES (?, ?, 0, ?)",
        (topic_id, name, ancestor_path),
    )


def _place_chunk_in_topic(conn, chunk_id, topic_id, confidence=None):
    conn.execute(
        "UPDATE chunks SET topic_id = ?, topic_confidence = ? WHERE chunk_id = ?",
        (topic_id, confidence, chunk_id),
    )


def test_basic_search_returns_results_ranked_sensibly(conn):
    _seed_source(conn, "doc1")
    _seed_chunk(conn, "c1", "doc1", "apple banana smoothie")
    _index_fts(conn, "c1", "apple banana smoothie")
    _index_vector(conn, "c1", _vec(1.0))

    embedder = StubQueryEmbedder(_vec(1.0))
    result = _search_impl(conn, embedder, "apple", None, 10, None)

    assert isinstance(result, SearchResult)
    assert len(result.results) == 1
    assert result.results[0].chunk_id == "c1"
    assert result.results[0].score > 0


def test_chunk_in_both_lists_ranks_above_chunk_in_only_one(conn):
    _seed_source(conn, "doc1")
    _seed_source(conn, "doc2")
    _seed_chunk(conn, "both", "doc1", "unicorn rainbow")
    _seed_chunk(conn, "fts_only", "doc2", "unicorn sparkle")

    # Both chunks match the FTS query term "unicorn". Only "both" is
    # vector-indexed at all - "fts_only" has no chunk_vectors row, so it
    # can never get a vector rank, which is exactly the "missing from one
    # list contributes 0" case RRF is supposed to handle.
    _index_fts(conn, "both", "unicorn rainbow")
    _index_fts(conn, "fts_only", "unicorn sparkle")
    _index_vector(conn, "both", _vec(1.0))

    embedder = StubQueryEmbedder(_vec(1.0))
    result = _search_impl(conn, embedder, "unicorn", None, 10, None)

    ids_in_order = [r.chunk_id for r in result.results]
    assert ids_in_order == ["both", "fts_only"]
    scores = {r.chunk_id: r.score for r in result.results}
    assert scores["both"] > scores["fts_only"]


def _seed_rrf_tied_pair(conn):
    """Two chunks with symmetric (bm25 rank, vector rank) pairs -> an
    identical fused RRF score (issue #82):

      - "alpha": short doc, one "wombat" -> BM25 rank 1; vector _vec(0.5),
        squared-euclidean 0.25 from the query -> vector rank 2.
      - "omega": longer doc, one "wombat" -> BM25 rank 2; vector _vec(1.0),
        exact match -> vector rank 1.

    _rrf_score(1, 2) == _rrf_score(2, 1), so the two tie on score and the
    order is decided purely by the tiebreak.
    """
    _seed_source(conn, "d_alpha", source_id="/tmp/alpha")
    _seed_source(conn, "d_omega", source_id="/tmp/omega")
    _seed_chunk(conn, "alpha", "d_alpha", "wombat")
    _seed_chunk(conn, "omega", "d_omega", "wombat filler filler filler")
    _index_fts(conn, "alpha", "wombat")
    _index_fts(conn, "omega", "wombat filler filler filler")
    _index_vector(conn, "alpha", _vec(0.5))
    _index_vector(conn, "omega", _vec(1.0))
    conn.commit()


def test_rrf_score_tie_breaks_toward_better_vector_rank(conn):
    """issue #82: on a fused-score tie, the chunk the embedding ranked
    higher wins (a BM25 tie is likelier a spurious single-term match)."""
    _seed_rrf_tied_pair(conn)
    result = _search_impl(conn, StubQueryEmbedder(_vec(1.0)), "wombat", None, 10, None)

    ids = [r.chunk_id for r in result.results]
    assert ids == ["omega", "alpha"]                       # omega has vector rank 1
    scores = {r.chunk_id: r.score for r in result.results}
    assert scores["omega"] == scores["alpha"]              # genuinely tied on score


def test_rrf_tied_scores_resolve_identically_across_hash_seeds(conn, tmp_path):
    """issue #82 repro, same shape as the #54 UMAP determinism test:
    before the fix the winner of a fused-score tie was decided by
    set-iteration order over chunk_ids, so it flipped with PYTHONHASHSEED.
    Run _search_impl in a fresh process under a spread of seeds and
    confirm the top hit is identical every time."""
    _seed_rrf_tied_pair(conn)
    db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
    repo_root = Path(__file__).resolve().parent.parent

    probe = tmp_path / "rrf_probe.py"
    probe.write_text(
        "import sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from jarvis_server.internal import db\n"
        "from jarvis_server.internal.embed.embed import Embedder\n"
        "from jarvis_server.mcp_tools.search import _search_impl\n"
        "class _E(Embedder):\n"
        "    def embed(self, texts):\n"
        "        return [[1.0] + [0.0] * 1023 for _ in texts]\n"
        "conn = db.get_connection(sys.argv[2])\n"
        "r = _search_impl(conn, _E(), 'wombat', None, 10, None)\n"
        "print(r.results[0].chunk_id)\n"
    )

    winners = []
    for seed in range(12):
        env = {**os.environ, "PYTHONHASHSEED": str(seed), "PYTHONPATH": str(repo_root)}
        out = subprocess.run(
            [sys.executable, str(probe), str(repo_root), str(db_path)],
            capture_output=True, text=True, env=env, check=True,
        )
        winners.append(out.stdout.strip())

    assert winners == ["omega"] * 12, f"nondeterministic top hit across hash seeds: {winners}"


def test_source_path_populated_for_filesystem_only(conn):
    _seed_source(conn, "doc1", connector="filesystem", source_id="/tmp/abs/path.md")
    _seed_source(conn, "doc2", connector="notion", source_id="opaque-page-uuid")
    _seed_chunk(conn, "fs_chunk", "doc1", "hello")
    _seed_chunk(conn, "notion_chunk", "doc2", "hello")
    for cid, text in [("fs_chunk", "hello"), ("notion_chunk", "hello")]:
        _index_fts(conn, cid, text)
        _index_vector(conn, cid, _vec(1.0))

    embedder = StubQueryEmbedder(_vec(1.0))
    result = _search_impl(conn, embedder, "hello", None, 10, None)

    by_id = {r.chunk_id: r for r in result.results}
    assert by_id["fs_chunk"].sources[0].path == "/tmp/abs/path.md"
    assert by_id["notion_chunk"].sources[0].path is None


def test_source_modified_at_surfaced_on_the_search_result(conn):
    """DESIGN.md §5.3 / Figma parity pass - the search-result row's
    relative timestamp needs this field on SourceRef; confirms it's
    actually read back, not just accepted at insert time (_seed_source
    already passed source_modified_at before this phase - nothing
    surfaced it)."""
    _seed_source(conn, "doc1", source_modified_at="2026-03-14T00:00:00Z")
    _seed_chunk(conn, "chunk1", "doc1", "hello")
    _index_fts(conn, "chunk1", "hello")
    _index_vector(conn, "chunk1", _vec(1.0))

    embedder = StubQueryEmbedder(_vec(1.0))
    result = _search_impl(conn, embedder, "hello", None, 10, None)

    assert result.results[0].sources[0].source_modified_at == "2026-03-14T00:00:00Z"


def test_hard_filters_narrow_and_empty_results(conn):
    _seed_source(conn, "doc1", connector="filesystem")
    _seed_source(conn, "doc2", connector="notion")
    _seed_chunk(conn, "fs_chunk", "doc1", "quarterly report")
    _seed_chunk(conn, "notion_chunk", "doc2", "quarterly report")
    for cid, text in [("fs_chunk", "quarterly report"), ("notion_chunk", "quarterly report")]:
        _index_fts(conn, cid, text)
        _index_vector(conn, cid, _vec(1.0))

    embedder = StubQueryEmbedder(_vec(1.0))

    narrowed = _search_impl(conn, embedder, "quarterly", SearchFilters(source="filesystem"), 10, None)
    assert isinstance(narrowed, SearchResult)
    assert [r.chunk_id for r in narrowed.results] == ["fs_chunk"]

    empty = _search_impl(conn, embedder, "quarterly", SearchFilters(source="does_not_exist"), 10, None)
    assert isinstance(empty, SearchResult)
    assert empty.results == []
    assert empty.next_cursor is None


def test_cursor_pagination_returns_next_page(conn):
    _seed_source(conn, "doc1")
    for i, cid in enumerate(["c1", "c2", "c3"]):
        _seed_chunk(conn, cid, "doc1", f"filler text {i}")
        # No FTS indexing - query term deliberately matches nothing, so
        # ordering is driven purely by (deterministic) vector distance.
        _index_vector(conn, cid, _vec(float(i + 1)))  # c1 closest, c3 farthest

    conn.execute("INSERT INTO sync_state (id, current_sync_id) VALUES (1, 'sync-abc')")
    conn.commit()

    embedder = StubQueryEmbedder(_vec(1.0))

    first = _search_impl(conn, embedder, "zzz_no_fts_match", None, 1, None)
    assert isinstance(first, SearchResult)
    assert [r.chunk_id for r in first.results] == ["c1"]
    assert first.next_cursor is not None

    decoded = _decode_cursor(first.next_cursor)
    assert decoded == {"sync_id": "sync-abc", "offset": 1}

    second = _search_impl(conn, embedder, "zzz_no_fts_match", None, 1, first.next_cursor)
    assert isinstance(second, SearchResult)
    assert [r.chunk_id for r in second.results] == ["c2"]
    assert second.next_cursor is not None

    third = _search_impl(conn, embedder, "zzz_no_fts_match", None, 1, second.next_cursor)
    assert [r.chunk_id for r in third.results] == ["c3"]
    assert third.next_cursor is None  # no more pages


def test_cursor_with_mismatched_sync_id_returns_stale_cursor(conn):
    conn.execute("INSERT INTO sync_state (id, current_sync_id) VALUES (1, 'real-sync-1')")
    conn.commit()

    stale_cursor = _encode_cursor("old-sync-0", 0)
    embedder = StubQueryEmbedder(_vec(1.0))

    result = _search_impl(conn, embedder, "anything", None, 10, stale_cursor)

    assert isinstance(result, ErrorResponse)
    assert result.error.code == "stale_cursor"


def test_malformed_cursor_returns_invalid_argument(conn):
    embedder = StubQueryEmbedder(_vec(1.0))

    result = _search_impl(conn, embedder, "anything", None, 10, "not-valid-base64-or-json!!!")

    assert isinstance(result, ErrorResponse)
    assert result.error.code == "invalid_argument"


def test_cursor_valid_when_no_sync_state_row_exists_yet(conn):
    """Confirmed design: a fresh database with nothing ever embedded has
    no sync_state row at all - treated as sync_id=None, and a cursor
    referencing that is immediately valid, not stale."""
    _seed_source(conn, "doc1")
    _seed_chunk(conn, "c1", "doc1", "text")
    _index_vector(conn, "c1", _vec(1.0))

    cursor_with_null_sync = _encode_cursor(None, 0)
    embedder = StubQueryEmbedder(_vec(1.0))

    result = _search_impl(conn, embedder, "zzz_no_fts_match", None, 10, cursor_with_null_sync)

    assert isinstance(result, SearchResult)


def test_limit_is_capped_at_50_even_if_caller_requests_more(conn):
    _seed_source(conn, "doc1")
    for i in range(60):
        cid = f"c{i}"
        _seed_chunk(conn, cid, "doc1", "matching text")
        _index_fts(conn, cid, "matching text")
        _index_vector(conn, cid, _vec(float(i)))

    embedder = StubQueryEmbedder(_vec(1.0))
    result = _search_impl(conn, embedder, "matching", None, 1000, None)

    assert isinstance(result, SearchResult)
    assert len(result.results) == 50
    assert result.next_cursor is not None


# ---------------------------------------------------------------------
# Tombstone exclusion (always-on, regardless of filters)
# ---------------------------------------------------------------------

def test_tombstoned_only_source_is_never_returned_by_plain_search(conn):
    """Regression: a document whose sole provenance link is tombstoned must
    leave the searchable corpus (spec §5.2), even for an unfiltered
    search() call."""
    _seed_source(conn, "doc1")
    _seed_chunk(conn, "c1", "doc1", "archived confidential memo")
    _index_fts(conn, "c1", "archived confidential memo")
    _index_vector(conn, "c1", _vec(1.0))
    _tombstone_sources(conn, "doc1")

    embedder = StubQueryEmbedder(_vec(1.0))
    result = _search_impl(conn, embedder, "archived", None, 10, None)

    assert isinstance(result, SearchResult)
    assert result.results == []
    assert result.next_cursor is None


# ---------------------------------------------------------------------
# topic filter
# ---------------------------------------------------------------------

def test_topic_filter_by_uuid(conn):
    topic_id = "11111111-1111-1111-1111-111111111111"
    _seed_topic(conn, topic_id, "Databases", ancestor_path=json.dumps([topic_id]))
    _seed_source(conn, "doc1")
    _seed_source(conn, "doc2")
    _seed_chunk(conn, "in_topic", "doc1", "postgres indexing strategy")
    _seed_chunk(conn, "out_topic", "doc2", "postgres indexing strategy")
    _place_chunk_in_topic(conn, "in_topic", topic_id, confidence=0.83)
    for cid in ("in_topic", "out_topic"):
        _index_fts(conn, cid, "postgres indexing strategy")
        _index_vector(conn, cid, _vec(1.0))

    embedder = StubQueryEmbedder(_vec(1.0))
    result = _search_impl(conn, embedder, "postgres", SearchFilters(topic=topic_id), 10, None)

    assert isinstance(result, SearchResult)
    assert [r.chunk_id for r in result.results] == ["in_topic"]
    # J-10: _build_chunk_hit now fills in topic / confidence from the cache
    hit = result.results[0]
    assert hit.topic is not None and hit.topic.id == topic_id
    assert hit.topic.path == "Databases"
    assert hit.confidence == pytest.approx(0.83)


def test_chunk_hit_topic_is_none_for_an_unplaced_chunk(conn):
    _seed_source(conn, "doc1")
    _seed_chunk(conn, "c1", "doc1", "loose unfiled text")
    _index_vector(conn, "c1", _vec(1.0))

    embedder = StubQueryEmbedder(_vec(1.0))
    result = _search_impl(conn, embedder, "zzz_no_fts_match", None, 10, None)

    assert isinstance(result, SearchResult)
    assert result.results[0].topic is None
    assert result.results[0].confidence is None


def test_topic_filter_by_exact_name_fallback(conn):
    topic_id = "22222222-2222-2222-2222-222222222222"
    _seed_topic(conn, topic_id, "Machine Learning")
    _seed_source(conn, "doc1")
    _seed_source(conn, "doc2")
    _seed_chunk(conn, "in_topic", "doc1", "gradient descent notes")
    _seed_chunk(conn, "out_topic", "doc2", "gradient descent notes")
    _place_chunk_in_topic(conn, "in_topic", topic_id)
    for cid in ("in_topic", "out_topic"):
        _index_fts(conn, cid, "gradient descent notes")
        _index_vector(conn, cid, _vec(1.0))

    embedder = StubQueryEmbedder(_vec(1.0))
    result = _search_impl(
        conn, embedder, "gradient", SearchFilters(topic="Machine Learning"), 10, None
    )

    assert isinstance(result, SearchResult)
    assert [r.chunk_id for r in result.results] == ["in_topic"]


def test_topic_filter_name_matching_multiple_topics_uses_all(conn):
    topic_a = "33333333-3333-3333-3333-333333333333"
    topic_b = "44444444-4444-4444-4444-444444444444"
    _seed_topic(conn, topic_a, "Notes")
    _seed_topic(conn, topic_b, "Notes")
    _seed_source(conn, "doc1")
    _seed_source(conn, "doc2")
    _seed_source(conn, "doc3")
    _seed_chunk(conn, "in_a", "doc1", "meeting notes text")
    _seed_chunk(conn, "in_b", "doc2", "meeting notes text")
    _seed_chunk(conn, "unplaced", "doc3", "meeting notes text")
    _place_chunk_in_topic(conn, "in_a", topic_a)
    _place_chunk_in_topic(conn, "in_b", topic_b)
    for cid in ("in_a", "in_b", "unplaced"):
        _index_fts(conn, cid, "meeting notes text")
        _index_vector(conn, cid, _vec(1.0))

    embedder = StubQueryEmbedder(_vec(1.0))
    result = _search_impl(conn, embedder, "meeting", SearchFilters(topic="Notes"), 10, None)

    assert isinstance(result, SearchResult)
    assert {r.chunk_id for r in result.results} == {"in_a", "in_b"}


def test_topic_filter_matches_subtree_via_child_ancestor_path(conn):
    parent_id = "55555555-5555-5555-5555-555555555555"
    child_id = "66666666-6666-6666-6666-666666666666"
    _seed_topic(conn, parent_id, "Engineering")
    _seed_topic(conn, child_id, "Backend", ancestor_path=json.dumps([parent_id, child_id]))
    _seed_source(conn, "doc1")
    _seed_chunk(conn, "child_chunk", "doc1", "service deployment runbook")
    _place_chunk_in_topic(conn, "child_chunk", child_id)
    _index_fts(conn, "child_chunk", "service deployment runbook")
    _index_vector(conn, "child_chunk", _vec(1.0))

    embedder = StubQueryEmbedder(_vec(1.0))
    result = _search_impl(
        conn, embedder, "deployment", SearchFilters(topic=parent_id), 10, None
    )

    assert isinstance(result, SearchResult)
    assert [r.chunk_id for r in result.results] == ["child_chunk"]


# ---------------------------------------------------------------------
# date_range / file_type filters
# ---------------------------------------------------------------------

def test_date_range_filter_applies_both_bounds(conn):
    _seed_source(conn, "old", source_id="/tmp/old", source_modified_at="2025-06-01T00:00:00Z")
    _seed_source(conn, "mid", source_id="/tmp/mid", source_modified_at="2026-03-15T00:00:00Z")
    _seed_source(conn, "new", source_id="/tmp/new", source_modified_at="2027-01-01T00:00:00Z")
    for h in ("old", "mid", "new"):
        _seed_chunk(conn, f"c_{h}", h, "annual report figures")
        _index_fts(conn, f"c_{h}", "annual report figures")
        _index_vector(conn, f"c_{h}", _vec(1.0))

    embedder = StubQueryEmbedder(_vec(1.0))
    filters = SearchFilters(
        date_range=DateRange(
            from_date="2026-01-01T00:00:00Z", to_date="2026-12-31T23:59:59Z"
        )
    )
    result = _search_impl(conn, embedder, "annual", filters, 10, None)

    assert isinstance(result, SearchResult)
    assert [r.chunk_id for r in result.results] == ["c_mid"]


# ---------------------------------------------------------------------
# FTS5 query sanitization (punctuation must not be parsed as operators)
# ---------------------------------------------------------------------

def test_sanitize_fts5_query_ors_content_terms_and_drops_stopwords_and_short_terms():
    # issue #52: OR-joined (any word matches is enough, BM25 ranks), with
    # stopwords + sub-3-char terms filtered so near-universal words can't
    # dominate the keyword ranking.
    assert _sanitize_fts5_query("how does write-ahead logging work") == (
        '"write-ahead" OR "logging" OR "work"'          # how / does dropped
    )
    assert _sanitize_fts5_query("how smart is an octopus and why") == (
        '"smart" OR "octopus"'                          # how / is / an / and / why dropped
    )
    assert _sanitize_fts5_query('say "hello" now') == '"say" OR "hello" OR "now"'
    assert _sanitize_fts5_query("single") == '"single"'
    assert _sanitize_fts5_query("why do we") == ""      # all-stopword / all-short -> empty
    assert _sanitize_fts5_query('   """   ') == ""


def test_hyphenated_query_does_not_crash_and_ranks_sensibly(conn):
    _seed_source(conn, "doc1")
    _seed_source(conn, "doc2")
    _seed_chunk(conn, "wal", "doc1", "write-ahead logging keeps the database durable")
    _seed_chunk(conn, "unrelated", "doc2", "banana bread recipe")
    for cid, text in [
        ("wal", "write-ahead logging keeps the database durable"),
        ("unrelated", "banana bread recipe"),
    ]:
        _index_fts(conn, cid, text)
        _index_vector(conn, cid, _vec(1.0))

    embedder = StubQueryEmbedder(_vec(1.0))
    result = _search_impl(conn, embedder, "how does write-ahead logging work", None, 10, None)

    assert isinstance(result, SearchResult)
    assert result.results, "hyphenated query should return results, not an error"
    assert result.results[0].chunk_id == "wal"


def test_quote_containing_query_does_not_crash_and_ranks_sensibly(conn):
    _seed_source(conn, "doc1")
    _seed_source(conn, "doc2")
    _seed_chunk(conn, "wal", "doc1", "the write-ahead log is flushed on commit")
    _seed_chunk(conn, "unrelated", "doc2", "sunset photography tips")
    for cid, text in [
        ("wal", "the write-ahead log is flushed on commit"),
        ("unrelated", "sunset photography tips"),
    ]:
        _index_fts(conn, cid, text)
        _index_vector(conn, cid, _vec(1.0))

    embedder = StubQueryEmbedder(_vec(1.0))
    result = _search_impl(conn, embedder, 'what is a "write-ahead" log', None, 10, None)

    assert isinstance(result, SearchResult)
    assert result.results, "quoted query should return results, not an error"
    assert result.results[0].chunk_id == "wal"


def test_fts5_stemming_matches_inflected_forms(conn):
    """issue #52: Porter stemming (migration 0009) - a query for the
    singular matches the plural in the text, and vice versa."""
    _index_fts(conn, "c_ckpt", "the database runs periodic checkpoints to truncate the log")
    _index_fts(conn, "c_bake", "she was baking bread every weekend")

    assert _fts5_ranked_ids(conn, "checkpoint", None, 10) == {"c_ckpt": 1}
    assert _fts5_ranked_ids(conn, "bake", None, 10) == {"c_bake": 1}
    assert _fts5_ranked_ids(conn, "truncating", None, 10) == {"c_ckpt": 1}


def test_fts5_natural_language_question_still_hits_the_keyword_index(conn):
    """issue #52: OR-joined terms - a full-sentence question keeps a
    keyword match instead of falling through to vector-only. Under the old
    implicit-AND every word had to be present, so this returned nothing."""
    _index_fts(conn, "c_ckpt", "a checkpoint copies WAL frames back into the main database file")
    _index_fts(conn, "c_octo", "octopus arms have substantial local neural autonomy")

    ranked = _fts5_ranked_ids(conn, "how often does a checkpoint actually run", None, 10)
    assert "c_ckpt" in ranked            # matched on "checkpoint" / "run" alone
    assert "c_octo" not in ranked        # shares no content words with the question


def test_file_type_filter_matches_mime(conn):
    # Isolated filter-SQL check with mime seeded directly. The connector
    # actually populating document_sources.mime on a real ingest, and the
    # filter narrowing real results, is covered end to end by
    # test_pipeline.py::test_file_type_filter_works_end_to_end_on_real_mime
    # (issue #51).
    _seed_source(conn, "pdf_doc", source_id="/tmp/a.pdf", mime="application/pdf")
    _seed_source(conn, "md_doc", source_id="/tmp/b.md", mime="text/markdown")
    _seed_chunk(conn, "pdf_chunk", "pdf_doc", "invoice line items")
    _seed_chunk(conn, "md_chunk", "md_doc", "invoice line items")
    for cid in ("pdf_chunk", "md_chunk"):
        _index_fts(conn, cid, "invoice line items")
        _index_vector(conn, cid, _vec(1.0))

    embedder = StubQueryEmbedder(_vec(1.0))
    result = _search_impl(
        conn, embedder, "invoice", SearchFilters(file_type="application/pdf"), 10, None
    )

    assert isinstance(result, SearchResult)
    assert [r.chunk_id for r in result.results] == ["pdf_chunk"]


# ---------------------------------------------------------------------
# search()'s own error-code mapping - the wrapper around _search_impl,
# not _search_impl itself. Real, confirmed bug: an embedding-provider
# auth failure (HTTP 401/403) was mislabeled "invalid_argument" - the
# same code used for a genuinely malformed query - misleading, since a
# missing/wrong/expired EMBEDDING_API_KEY is a service-configuration
# problem, not anything about what the caller sent. Fixed to
# "internal_error" - this project's own established code for exactly
# that distinction (already used identically in sql_query.py), and the
# closest real MCP SDK equivalent (mcp.types.INTERNAL_ERROR, -32603),
# checked directly rather than guessed.
# ---------------------------------------------------------------------

class _RaisingEmbedder(Embedder):
    def __init__(self, exc: Exception):
        self._exc = exc

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise self._exc


def test_search_auth_failure_returns_internal_error_not_invalid_argument(conn, monkeypatch):
    # _search_impl short-circuits to an empty SearchResult (never
    # reaching embedder.embed() at all) when the candidate set is empty -
    # a real chunk has to exist so the code actually gets far enough to
    # call the embedder and hit the auth-failure path being tested.
    _seed_source(conn, "doc1")
    _seed_chunk(conn, "chunk1", "doc1", "some real content")

    monkeypatch.setattr(search_mod.db, "get_connection", lambda: conn)
    auth_failure = RuntimeError(
        "Embedding API rejected the request (HTTP 401). Check that "
        "EMBEDDING_API_KEY in your .env is set to a valid OpenRouter key "
        "(see the original error below for the provider's response)."
    )
    monkeypatch.setattr(search_mod, "OpenRouterVoyageEmbedder", lambda: _RaisingEmbedder(auth_failure))

    result = search("test query")

    assert isinstance(result, ErrorResponse)
    assert result.error.code == "internal_error"


def test_other_unexpected_search_errors_keep_invalid_argument_unchanged(conn, monkeypatch):
    """Confirms the fix is scoped to the auth-failure RuntimeError path
    only - any other, unrelated unexpected exception (not raised by the
    embedding provider's own auth handling) keeps the exact code it
    already had before this fix."""
    _seed_source(conn, "doc1")
    _seed_chunk(conn, "chunk1", "doc1", "some real content")

    monkeypatch.setattr(search_mod.db, "get_connection", lambda: conn)
    monkeypatch.setattr(
        search_mod, "OpenRouterVoyageEmbedder",
        lambda: _RaisingEmbedder(ValueError("something else went wrong, unrelated to auth")),
    )

    result = search("test query")

    assert isinstance(result, ErrorResponse)
    assert result.error.code == "invalid_argument"
