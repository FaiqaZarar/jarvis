"""J-06 acceptance check: embedding cache-check, chunk_vectors/chunks_fts
indexing, and sqlite-vec KNN ranking. No network calls - uses a stub
Embedder throughout. See tests/test_embed_live.py for the real-API test."""

import sqlite3
import struct
from pathlib import Path

import pytest
import requests

from jarvis_server.internal import db
from jarvis_server.internal.embed import provider
from jarvis_server.internal.embed.embed import Embedder, IngestCancelled, embed_and_index_chunks
from jarvis_server.internal.embed.provider import OpenRouterVoyageEmbedder
from jarvis_server.internal.migrate import run_migrations


class StubEmbedder(Embedder):
    """Deterministic, no-network embedder for tests. Each text's vector is
    derived from its position in the batch, distinct enough to assert on."""

    def __init__(self, batch_size: int = 100):
        self.batch_size = batch_size
        self.call_count = 0
        self.texts_seen: list[str] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.call_count += 1
        self.texts_seen.extend(texts)
        return [[float(i + 1)] + [0.0] * 1023 for i in range(len(texts))]


def _to_blob(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


@pytest.fixture
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    connection = db.get_connection(db_path)
    yield connection
    connection.close()


def _insert_chunk(conn: sqlite3.Connection, chunk_id: str, text: str) -> None:
    conn.execute(
        "INSERT INTO documents (content_hash, normalized_markdown, first_seen_at, last_ingested_at) "
        "VALUES (?, '# T', 'now', 'now') ON CONFLICT DO NOTHING",
        (f"doc-{chunk_id}",),
    )
    conn.execute(
        "INSERT INTO chunks (chunk_id, content_hash, ordinal, chunk_hash, text) VALUES (?, ?, 0, ?, ?)",
        (chunk_id, f"doc-{chunk_id}", chunk_id, text),
    )
    conn.commit()


def test_fresh_chunk_gets_embedded_and_inserted(conn):
    _insert_chunk(conn, "c1", "hello world")
    embedder = StubEmbedder()

    stats = embed_and_index_chunks(conn, embedder)

    assert stats == {
        "total_chunks": 1, "cache_hits": 0, "newly_embedded": 1, "api_calls_made": 1,
    }
    assert embedder.call_count == 1
    vector_row = conn.execute("SELECT chunk_id FROM chunk_vectors WHERE chunk_id = 'c1'").fetchone()
    assert vector_row is not None
    fts_row = conn.execute("SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH 'hello'").fetchall()
    assert [r["chunk_id"] for r in fts_row] == ["c1"]


def test_cache_check_skips_already_embedded_chunks(conn):
    _insert_chunk(conn, "c1", "already embedded")
    _insert_chunk(conn, "c2", "brand new")
    embedder = StubEmbedder()
    # Pre-populate c1 as if a prior run already embedded it.
    conn.execute(
        "INSERT INTO chunk_vectors (chunk_id, embedding) VALUES ('c1', ?)",
        (_to_blob([9.0] + [0.0] * 1023),),
    )
    conn.commit()

    stats = embed_and_index_chunks(conn, embedder)

    assert stats["total_chunks"] == 2
    assert stats["cache_hits"] == 1
    assert stats["newly_embedded"] == 1
    assert embedder.texts_seen == ["brand new"]  # c1's text was never sent


def test_rerunning_twice_makes_zero_new_api_calls_the_second_time(conn):
    """J-06's stated acceptance criterion: re-running on unchanged data
    produces zero newly_embedded / zero new API calls."""
    _insert_chunk(conn, "c1", "one")
    _insert_chunk(conn, "c2", "two")
    embedder = StubEmbedder()

    first = embed_and_index_chunks(conn, embedder)
    assert first["newly_embedded"] == 2
    assert embedder.call_count == 1

    second = embed_and_index_chunks(conn, embedder)
    assert second == {
        "total_chunks": 2, "cache_hits": 2, "newly_embedded": 0, "api_calls_made": 0,
    }
    assert embedder.call_count == 1  # unchanged - no new API call happened


def test_on_batch_reports_an_initial_call_then_one_per_batch(conn):
    for i in range(5):
        _insert_chunk(conn, f"c{i}", f"text {i}")
    embedder = StubEmbedder(batch_size=2)
    calls = []

    embed_and_index_chunks(conn, embedder, on_batch=lambda *args: calls.append(args))

    # Initial call before the loop starts (current_batch=0), then one per
    # batch: ceil(5/2) = 3 batches of sizes 2, 2, 1.
    assert calls == [
        (0, 3, 0, 5),
        (1, 3, 2, 5),
        (2, 3, 4, 5),
        (3, 3, 5, 5),
    ]


def test_on_batch_not_called_on_a_pure_cache_hit_run(conn):
    _insert_chunk(conn, "c1", "already embedded")
    conn.execute(
        "INSERT INTO chunk_vectors (chunk_id, embedding) VALUES ('c1', ?)",
        (_to_blob([9.0] + [0.0] * 1023),),
    )
    conn.commit()
    calls = []

    embed_and_index_chunks(conn, StubEmbedder(), on_batch=lambda *args: calls.append(args))

    assert calls == []


def test_dimension_mismatch_raises_and_rolls_back(conn):
    """A model swap that returns the wrong dimension must fail loudly,
    not silently corrupt chunk_vectors (schema is fixed at FLOAT[1024])."""

    class WrongDimensionEmbedder(Embedder):
        batch_size = 100

        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[0.0] * 512 for _ in texts]  # wrong - table expects 1024

    _insert_chunk(conn, "c1", "will not be embedded")

    with pytest.raises(ValueError, match="embedding dimension mismatch"):
        embed_and_index_chunks(conn, WrongDimensionEmbedder())

    assert conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] == 0


# ---------------------------------------------------------------------
# ingest_run_phases (migration 0016) - per-batch commit + failure status.
# embed_and_index_chunks only writes progress when given a run_id, and
# only updates an existing row (pipeline.run_ingest's _begin_phase always
# inserts one first) - these tests set that up explicitly rather than
# relying on embed_and_index_chunks to create its own phase row.
# ---------------------------------------------------------------------

def _begin_run_and_phase(conn, run_id="run-1", phase="embed"):
    db.insert_ingest_run(conn, run_id, "filesystem", "/watched", "gui", "2026-09-21T00:00:00+00:00")
    db.insert_ingest_run_phase(conn, run_id, phase, "2026-09-21T00:00:00+00:00")


def test_ingest_run_phase_reflects_in_progress_through_a_full_success(conn):
    """finish_ingest_run_phase (status='done') is pipeline.py's job, not
    embed_and_index_chunks's - see _run_ingest_phases. This only confirms
    what embed_and_index_chunks itself writes: live counts, status still
    'in_progress' throughout."""
    for i in range(5):
        _insert_chunk(conn, f"c{i}", f"text {i}")
    _begin_run_and_phase(conn)

    embed_and_index_chunks(conn, StubEmbedder(batch_size=2), run_id="run-1", phase="embed")

    row = db.get_ingest_run_phases(conn, "run-1")[0]
    assert row["status"] == db.PHASE_STATUS_IN_PROGRESS
    assert row["total_items"] == 5
    assert row["processed_items"] == 5
    assert row["current_batch"] == row["total_batches"] == 3


def test_ingest_run_phase_is_visible_to_a_second_connection_mid_run(conn, tmp_path: Path):
    """The whole point of per-batch commits: a second connection to the
    same db file must see a batch's progress as soon as that batch
    completes, not only after the entire run finishes."""
    for i in range(3):
        _insert_chunk(conn, f"c{i}", f"text {i}")
    _begin_run_and_phase(conn)
    second_conn = db.get_connection(tmp_path / "test.db")
    seen_mid_run = []

    def _on_batch(current_batch, total_batches, processed_items, total_items):
        if current_batch > 0:
            seen_mid_run.append(dict(db.get_ingest_run_phases(second_conn, "run-1")[0]))

    embed_and_index_chunks(
        conn, StubEmbedder(batch_size=1), on_batch=_on_batch, run_id="run-1", phase="embed",
    )
    second_conn.close()

    assert len(seen_mid_run) == 3
    assert [row["current_batch"] for row in seen_mid_run] == [1, 2, 3]
    assert seen_mid_run[0]["status"] == db.PHASE_STATUS_IN_PROGRESS  # not finished yet
    assert seen_mid_run[-1]["status"] == db.PHASE_STATUS_IN_PROGRESS  # pipeline.py, not this call, marks it done


def test_a_failed_later_batch_keeps_an_earlier_batch_s_commit(conn):
    """The core trade-off of per-batch commits: batch 1 embeds fine and
    commits; batch 2 hits a dimension mismatch. Batch 1's work must
    survive (not be rolled back with batch 2's), and the progress row
    must reflect only what actually committed."""

    class FailsOnSecondCallEmbedder(Embedder):
        batch_size = 1

        def __init__(self):
            self.calls = 0

        def embed(self, texts: list[str]) -> list[list[float]]:
            self.calls += 1
            if self.calls == 2:
                return [[0.0] * 512 for _ in texts]  # wrong dimension
            return [[1.0] + [0.0] * 1023 for _ in texts]

    _insert_chunk(conn, "c1", "will embed fine")
    _insert_chunk(conn, "c2", "will fail")
    _begin_run_and_phase(conn)

    with pytest.raises(ValueError, match="embedding dimension mismatch"):
        embed_and_index_chunks(conn, FailsOnSecondCallEmbedder(), run_id="run-1", phase="embed")

    # batch 1's chunk survived - not discarded along with batch 2's failure
    assert conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0] == 1
    assert conn.execute(
        "SELECT chunk_id FROM chunk_vectors"
    ).fetchone()["chunk_id"] == "c1"

    row = db.get_ingest_run_phases(conn, "run-1")[0]
    assert row["processed_items"] == 1  # only batch 1's item, not batch 2's
    assert row["current_batch"] == 1  # the last batch that actually committed
    assert row["total_batches"] == 2


def test_on_cancel_requested_stops_between_batches_not_mid_batch(conn):
    for i in range(3):
        _insert_chunk(conn, f"c{i}", f"text {i}")
    _begin_run_and_phase(conn)
    calls = []

    def _cancel_after_first_batch():
        return len(calls) >= 1

    def _on_batch(*args):
        calls.append(args)

    with pytest.raises(IngestCancelled) as exc_info:
        embed_and_index_chunks(
            conn, StubEmbedder(batch_size=1), on_batch=_on_batch,
            run_id="run-1", phase="embed", on_cancel_requested=_cancel_after_first_batch,
        )

    assert exc_info.value.partial["newly_embedded"] == 1  # only the first batch ran
    assert conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0] == 1  # its work survives


def test_on_cancel_requested_is_not_checked_after_the_final_batch(conn):
    """A cancel flag that flips true only after the last batch commits
    must not turn a fully-successful embed into a cancelled one."""
    _insert_chunk(conn, "c1", "only chunk")
    _begin_run_and_phase(conn)

    stats = embed_and_index_chunks(
        conn, StubEmbedder(batch_size=10), run_id="run-1", phase="embed",
        on_cancel_requested=lambda: True,
    )

    assert stats["newly_embedded"] == 1


def test_knn_query_ranks_by_actual_distance(conn):
    """Mirrors the earlier-verified ranking check: identical vector is
    distance 0, a near vector ranks second, an orthogonal vector ranks
    last."""
    query = [1.0] + [0.0] * 1023
    identical = query
    near = [0.9] + [0.1] * 3 + [0.0] * 1020
    orthogonal = [0.0, 1.0] + [0.0] * 1022

    conn.execute("INSERT INTO chunk_vectors (chunk_id, embedding) VALUES ('identical', ?)", (_to_blob(identical),))
    conn.execute("INSERT INTO chunk_vectors (chunk_id, embedding) VALUES ('near', ?)", (_to_blob(near),))
    conn.execute("INSERT INTO chunk_vectors (chunk_id, embedding) VALUES ('orthogonal', ?)", (_to_blob(orthogonal),))
    conn.commit()

    rows = conn.execute(
        "SELECT chunk_id, distance FROM chunk_vectors WHERE embedding MATCH ? ORDER BY distance LIMIT 5",
        (_to_blob(query),),
    ).fetchall()

    ranked_ids = [r["chunk_id"] for r in rows]
    assert ranked_ids == ["identical", "near", "orthogonal"]
    assert rows[0]["distance"] == 0.0
    assert rows[1]["distance"] < rows[2]["distance"]


# ---------------------------------------------------------------------
# Auth-failure error clarity (a wrong EMBEDDING_API_KEY -> HTTP 401)
# ---------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        err = requests.HTTPError(f"{self.status_code} Client Error: Unauthorized for url: ...")
        err.response = self
        raise err

    def json(self):  # pragma: no cover - not reached on the 401 path
        return {}


def test_auth_failure_raises_actionable_error_without_hiding_the_original(monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-wrong")
    monkeypatch.setenv("EMBEDDING_MODEL", "voyageai/voyage-4")
    monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResponse(401))

    with pytest.raises(RuntimeError) as excinfo:
        OpenRouterVoyageEmbedder().embed(["some text"])

    msg = str(excinfo.value)
    assert "EMBEDDING_API_KEY" in msg
    assert "401" in msg
    # the raw provider error is chained, not swallowed
    assert isinstance(excinfo.value.__cause__, requests.HTTPError)
    assert "Unauthorized" in str(excinfo.value.__cause__)


def test_non_auth_http_error_is_re_raised_unchanged(monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-ok")
    monkeypatch.setenv("EMBEDDING_MODEL", "voyageai/voyage-4")
    monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResponse(500))

    with pytest.raises(requests.HTTPError):   # not wrapped - only 401/403 get the rewrite
        OpenRouterVoyageEmbedder().embed(["some text"])


# ---------------------------------------------------------------------
# Transient-connection-failure retry (a real ConnectionResetError was
# observed mid-TLS-handshake against openrouter.ai during a live ingest
# - see jarvis/internal/embed/provider.py's _embed_batch).
# ---------------------------------------------------------------------

class _FakeEmbedResponse:
    """A successful embeddings response - `raise_for_status()` is a
    no-op, `json()` returns one vector per input text."""

    def __init__(self, n: int):
        self._n = n

    def raise_for_status(self):
        pass

    def json(self):
        return {"data": [{"embedding": [0.0] * 1024} for _ in range(self._n)]}


def _counting_post(side_effects):
    """Fake `requests.post` that pops one entry off `side_effects` per
    call - an exception instance (raised) or a response-like object
    (returned). Returns (fake_post, calls) where `calls` is a plain
    counter list."""
    calls = []

    def _fake_post(*args, **kwargs):
        calls.append(1)
        effect = side_effects[len(calls) - 1]
        if isinstance(effect, Exception):
            raise effect
        return effect

    return _fake_post, calls


def test_embed_batch_retries_on_transient_connection_error_then_succeeds(monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-ok")
    monkeypatch.setenv("EMBEDDING_MODEL", "voyageai/voyage-4")
    monkeypatch.setattr(provider, "_EMBED_RETRY_DELAY_S", 0.0)
    fake_post, calls = _counting_post([
        requests.exceptions.ConnectionError("reset"),
        _FakeEmbedResponse(1),
    ])
    monkeypatch.setattr(requests, "post", fake_post)

    vectors = OpenRouterVoyageEmbedder().embed(["some text"])

    assert vectors == [[0.0] * 1024]
    assert len(calls) == 2  # one failure + the retry that succeeded


def test_embed_batch_retries_on_timeout_then_succeeds(monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-ok")
    monkeypatch.setenv("EMBEDDING_MODEL", "voyageai/voyage-4")
    monkeypatch.setattr(provider, "_EMBED_RETRY_DELAY_S", 0.0)
    fake_post, calls = _counting_post([
        requests.exceptions.Timeout("slow"),
        _FakeEmbedResponse(1),
    ])
    monkeypatch.setattr(requests, "post", fake_post)

    vectors = OpenRouterVoyageEmbedder().embed(["some text"])

    assert vectors == [[0.0] * 1024]
    assert len(calls) == 2


def test_embed_batch_gives_up_after_max_retries_on_persistent_connection_error(monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-ok")
    monkeypatch.setenv("EMBEDDING_MODEL", "voyageai/voyage-4")
    monkeypatch.setattr(provider, "_EMBED_RETRY_DELAY_S", 0.0)
    monkeypatch.setattr(provider, "_EMBED_MAX_RETRIES", 2)
    fake_post, calls = _counting_post([
        requests.exceptions.ConnectionError("reset"),
        requests.exceptions.ConnectionError("reset"),
        requests.exceptions.ConnectionError("reset"),
    ])
    monkeypatch.setattr(requests, "post", fake_post)

    with pytest.raises(requests.exceptions.ConnectionError):
        OpenRouterVoyageEmbedder().embed(["some text"])

    assert len(calls) == 3  # 1 original attempt + 2 retries, not a loop


def test_embed_batch_does_not_retry_auth_failures(monkeypatch):
    """A 401/403 is a real, deterministic rejection - retrying would just
    waste time hitting the same wrong key again."""
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-wrong")
    monkeypatch.setenv("EMBEDDING_MODEL", "voyageai/voyage-4")
    fake_post, calls = _counting_post([_FakeResponse(401)])
    monkeypatch.setattr(requests, "post", fake_post)

    with pytest.raises(RuntimeError):
        OpenRouterVoyageEmbedder().embed(["some text"])

    assert len(calls) == 1


def test_embed_batch_does_not_retry_other_http_errors(monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-ok")
    monkeypatch.setenv("EMBEDDING_MODEL", "voyageai/voyage-4")
    fake_post, calls = _counting_post([_FakeResponse(500)])
    monkeypatch.setattr(requests, "post", fake_post)

    with pytest.raises(requests.HTTPError):
        OpenRouterVoyageEmbedder().embed(["some text"])

    assert len(calls) == 1


def test_embed_splits_requests_by_total_characters_not_just_item_count(monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-ok")
    monkeypatch.setenv("EMBEDDING_MODEL", "voyageai/voyage-4")
    embedder = OpenRouterVoyageEmbedder(batch_size=100)
    big = "x" * (provider._MAX_BATCH_CHARS // 2 + 1)

    batches = embedder._split_batches([big, big, "small", big])

    assert [len(b) for b in batches] == [1, 2, 1]
    assert [t for b in batches for t in b] == [big, big, "small", big]


def test_embed_sends_an_oversized_single_chunk_alone(monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-ok")
    monkeypatch.setenv("EMBEDDING_MODEL", "voyageai/voyage-4")
    huge = "x" * (provider._MAX_BATCH_CHARS + 1)

    batches = OpenRouterVoyageEmbedder()._split_batches(["a", huge, "b"])

    assert batches == [["a"], [huge], ["b"]]


def test_embed_http_error_logs_the_providers_response_body(monkeypatch, caplog):
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-ok")
    monkeypatch.setenv("EMBEDDING_MODEL", "voyageai/voyage-4")
    monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResponse(400, "too many tokens in request"))

    with pytest.raises(requests.HTTPError):
        OpenRouterVoyageEmbedder().embed(["some text"])

    assert any("too many tokens in request" in r.getMessage() for r in caplog.records)
