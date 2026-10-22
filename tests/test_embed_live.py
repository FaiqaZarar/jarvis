"""J-06 live-API check: real call to Voyage via OpenRouter.

Not part of the normal suite - requires a real EMBEDDING_API_KEY in the
environment (see .env.example) and makes a real, billed network call.
Run explicitly:

    pytest tests/test_embed_live.py -v -m live_api --run-live
"""

import os
import struct

import pytest

from jarvis_server.internal.embed.embed import EMBEDDING_DIMENSIONS
from jarvis_server.internal.embed.provider import OpenRouterVoyageEmbedder

pytestmark = pytest.mark.live_api


@pytest.fixture(autouse=True)
def _require_run_live_flag(request):
    if not request.config.getoption("--run-live"):
        pytest.skip("live-API test skipped - pass --run-live to actually call OpenRouter")


@pytest.fixture(autouse=True)
def _require_api_key():
    if not os.environ.get("EMBEDDING_API_KEY"):
        pytest.skip("EMBEDDING_API_KEY not set - cannot make a real call")


def test_real_embedding_call_returns_correct_dimensions_and_is_storable():
    embedder = OpenRouterVoyageEmbedder()

    vectors = embedder.embed(["Jarvis MVP live embedding smoke test"])

    assert len(vectors) == 1
    vector = vectors[0]
    assert len(vector) == EMBEDDING_DIMENSIONS, (
        f"expected {EMBEDDING_DIMENSIONS} dims, got {len(vector)} - "
        "model or dimensions param may have drifted upstream"
    )
    assert any(v != 0.0 for v in vector), "vector is all zeros - suspicious response"

    # Round-trip through the same blob format embed_and_index_chunks uses,
    # confirm it's genuinely storable/queryable via sqlite-vec KNN.
    import sqlite3

    import sqlite_vec

    conn = sqlite3.connect(":memory:")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute(
        f"CREATE VIRTUAL TABLE chunk_vectors USING vec0(chunk_id TEXT PRIMARY KEY, embedding FLOAT[{EMBEDDING_DIMENSIONS}])"
    )
    blob = struct.pack(f"{len(vector)}f", *vector)
    conn.execute("INSERT INTO chunk_vectors (chunk_id, embedding) VALUES ('live1', ?)", (blob,))
    row = conn.execute(
        "SELECT chunk_id, distance FROM chunk_vectors WHERE embedding MATCH ? ORDER BY distance LIMIT 1",
        (blob,),
    ).fetchone()
    assert row[0] == "live1"
    assert row[1] == 0.0
