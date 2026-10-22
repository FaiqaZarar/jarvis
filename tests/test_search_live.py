"""J-07 live-API check: a real end-to-end search() call using the real
OpenRouterVoyageEmbedder against a small seeded database.

Not part of the normal suite - requires a real EMBEDDING_API_KEY in the
environment (see .env.example) and makes a real, billed network call.
Run explicitly:

    pytest tests/test_search_live.py -v -m live_api --run-live
"""

import os
from pathlib import Path

import pytest

from jarvis_server.internal import db
from jarvis_server.internal.embed.embed import _vector_to_blob
from jarvis_server.internal.embed.provider import OpenRouterVoyageEmbedder
from jarvis_server.internal.migrate import run_migrations
from jarvis_server.mcp_tools.models import SearchResult
from jarvis_server.mcp_tools.search import _search_impl

pytestmark = pytest.mark.live_api


@pytest.fixture(autouse=True)
def _require_run_live_flag(request):
    if not request.config.getoption("--run-live"):
        pytest.skip("live-API test skipped - pass --run-live to actually call OpenRouter")


@pytest.fixture(autouse=True)
def _require_api_key():
    if not os.environ.get("EMBEDDING_API_KEY"):
        pytest.skip("EMBEDDING_API_KEY not set - cannot make a real call")


def test_real_search_call_returns_sensible_results(tmp_path: Path):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    conn = db.get_connection(db_path)

    conn.execute(
        "INSERT INTO documents (content_hash, normalized_markdown, first_seen_at, last_ingested_at) "
        "VALUES ('doc1', '# T', 'now', 'now')"
    )
    db.insert_document_source(
        conn, source_id="/tmp/notes.md", connector="filesystem", content_hash="doc1",
        source_last_seen="now", title="notes.md",
    )
    conn.execute(
        "INSERT INTO chunks (chunk_id, content_hash, ordinal, chunk_hash, text) "
        "VALUES ('c1', 'doc1', 0, 'c1', 'The Eiffel Tower is a famous landmark in Paris, France.')"
    )
    conn.execute(
        "INSERT INTO chunks_fts (chunk_id, text) VALUES ('c1', 'The Eiffel Tower is a famous landmark in Paris, France.')"
    )

    embedder = OpenRouterVoyageEmbedder()
    real_vector = embedder.embed(["The Eiffel Tower is a famous landmark in Paris, France."])[0]
    conn.execute(
        "INSERT INTO chunk_vectors (chunk_id, embedding) VALUES ('c1', ?)",
        (_vector_to_blob(real_vector),),
    )
    conn.commit()

    result = _search_impl(conn, embedder, "famous landmark in Paris", None, 10, None)

    assert isinstance(result, SearchResult)
    assert len(result.results) == 1
    hit = result.results[0]
    assert hit.chunk_id == "c1"
    assert "Eiffel Tower" in hit.text
    assert 0.0 <= hit.score <= 1.0
    assert hit.document.document_id == "doc1"
    assert hit.document.title == "notes.md"
    assert len(hit.sources) == 1
    assert hit.sources[0].connector == "filesystem"

    conn.close()
