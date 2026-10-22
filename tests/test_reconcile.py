"""J-09 reconciliation - end to end against a real SQLite DB + LadybugDB.

Hand-built OrganizeResults (no UMAP/LLM) so the matching cardinality is
exact and the assertions are about what reconcile persists: topic rows,
the append-only event log, and the swapped-in graph.
"""

import struct

import numpy as np
import pytest

from jarvis_server.internal import db
from jarvis_server.internal.embed.embed import _vector_to_blob
from jarvis_server.internal.migrate import run_migrations
from jarvis_server.organize import config, reconcile
from jarvis_server.organize.clustering import ClusterNode, OrganizeResult

DIM = 1024   # fixed by migrations/0006 (sqlite-vec chunk_vectors FLOAT[1024])


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "reconcile.db"
    run_migrations(path)
    c = db.get_connection(path)
    yield c
    c.close()


@pytest.fixture
def graph_path(tmp_path):
    return tmp_path / "reconcile_graph.lbdb"


def _unit(weights):
    """A unit DIM-vector with the given leading components, zero elsewhere."""
    v = np.zeros(DIM, dtype=float)
    v[: len(weights)] = weights
    return (v / np.linalg.norm(v)).tolist()


def _seed_chunks(conn, chunk_vecs: dict[str, list[float]]):
    for i, (cid, vec) in enumerate(chunk_vecs.items()):
        conn.execute(
            "INSERT INTO chunks (chunk_id, content_hash, ordinal, chunk_hash, text) "
            "VALUES (?, ?, 0, ?, ?)",
            (cid, f"h{i}", cid, f"text of {cid}"),
        )
        conn.execute(
            "INSERT INTO chunk_vectors (chunk_id, embedding) VALUES (?, ?)",
            (cid, _vector_to_blob(vec)),
        )
    conn.commit()


def _centroid(chunk_vecs, ids):
    return np.mean([chunk_vecs[c] for c in ids], axis=0).tolist()


def _organize_result(chunk_vecs, clusters: dict[str, list[str]], unfiled: list[str] = ()):
    """clusters: {label -> [chunk_ids]}. Flat, single level, each a root."""
    nodes = []
    for label, ids in clusters.items():
        nodes.append(ClusterNode(
            key=f"base-{label}", level=0, label=label, description=f"{label} desc",
            centroid=_centroid(chunk_vecs, ids),
            member_chunk_ids=list(ids), direct_chunk_ids=list(ids),
            parent_key=None, child_keys=[],
        ))
    total = sum(len(v) for v in clusters.values()) + len(unfiled)
    unfiled_node = ClusterNode(
        key=config.UNFILED_TOPIC_ID, level=0, label="Unfiled", description="",
        centroid=_centroid(chunk_vecs, unfiled) if unfiled else [],
        member_chunk_ids=list(unfiled), direct_chunk_ids=list(unfiled),
    )
    return OrganizeResult(
        nodes=nodes, root_keys=[n.key for n in nodes], unfiled=unfiled_node,
        unfiled_topic_id=config.UNFILED_TOPIC_ID,
        outlier_scores={c: 0.0 for c in chunk_vecs}, unfiled_pct=0.0,
        total_chunks=total, used_kmeans_fallback=False,
    )


@pytest.fixture
def base_corpus():
    """Two well-separated clusters + one unfiled chunk."""
    vecs = {
        "c1": _unit([1, 0, 0, 0, 0, 0, 0, 0]),
        "c2": _unit([0.98, 0.05, 0, 0, 0, 0, 0, 0]),
        "c3": _unit([0.97, 0.10, 0, 0, 0, 0, 0, 0]),
        "d1": _unit([0, 1, 0, 0, 0, 0, 0, 0]),
        "d2": _unit([0, 0.98, 0.05, 0, 0, 0, 0, 0]),
        "d3": _unit([0, 0.97, 0.10, 0, 0, 0, 0, 0]),
        "x1": _unit([0, 0, 0, 0, 0, 0, 1, 0]),
    }
    return vecs


# ---------------------------------------------------------------------
# first sync
# ---------------------------------------------------------------------

def test_first_sync_borns_every_topic_and_swaps_the_graph(conn, graph_path, base_corpus):
    _seed_chunks(conn, base_corpus)
    result = _organize_result(base_corpus, {"Cats": ["c1", "c2", "c3"], "Dogs": ["d1", "d2", "d3"]},
                              unfiled=["x1"])

    r = reconcile.reconcile(conn, result, sync_id="sync-1", live_graph_path=graph_path)

    assert r.event_counts == {"born": 2}
    assert len(r.born_topic_ids) == 2
    assert r.graph_swapped and graph_path.exists()

    # topics: 2 born + reserved root + reserved Unfiled, none tombstoned
    rows = conn.execute("SELECT id, name, reserved, tombstoned, version FROM topics").fetchall()
    by_name = {row["name"]: row for row in rows}
    assert set(by_name) == {"Cats", "Dogs", config.ROOT_TOPIC_NAME, config.UNFILED_TOPIC_NAME}
    assert by_name["Cats"]["version"] == 1
    assert all(row["tombstoned"] == 0 for row in rows)

    # event log: one born per topic, keyed to this sync
    events = conn.execute("SELECT topic_id, sync_id, event_type FROM topic_events").fetchall()
    assert {e["event_type"] for e in events} == {"born"}
    assert {e["sync_id"] for e in events} == {"sync-1"}

    # every chunk placed; cache synced
    placed = conn.execute("SELECT chunk_id, topic_id FROM chunks WHERE topic_id IS NOT NULL").fetchall()
    assert len(placed) == len(base_corpus)
    x1_topic = conn.execute("SELECT topic_id FROM chunks WHERE chunk_id = 'x1'").fetchone()["topic_id"]
    assert x1_topic == config.UNFILED_TOPIC_ID


def test_low_confidence_flag_is_passed_through(conn, graph_path, base_corpus):
    _seed_chunks(conn, base_corpus)
    result = _organize_result(base_corpus, {"Cats": ["c1", "c2", "c3"], "Dogs": ["d1", "d2", "d3"]},
                              unfiled=["x1"])
    result.low_confidence = True
    result.low_confidence_reason = "only 7 chunks"

    r = reconcile.reconcile(conn, result, sync_id="sync-1", live_graph_path=graph_path)
    assert r.low_confidence is True


# ---------------------------------------------------------------------
# second sync
# ---------------------------------------------------------------------

@pytest.fixture
def after_first_sync(conn, graph_path, base_corpus):
    _seed_chunks(conn, base_corpus)
    result = _organize_result(base_corpus, {"Cats": ["c1", "c2", "c3"], "Dogs": ["d1", "d2", "d3"]},
                              unfiled=["x1"])
    r = reconcile.reconcile(conn, result, sync_id="sync-1", live_graph_path=graph_path)
    ids = {row["name"]: row["id"] for row in conn.execute("SELECT id, name FROM topics").fetchall()}
    return ids


def test_unchanged_resync_produces_no_structural_events_and_keeps_uuids(
    conn, graph_path, base_corpus, after_first_sync
):
    cats_uuid = after_first_sync["Cats"]
    same = _organize_result(base_corpus, {"Cats": ["c1", "c2", "c3"], "Dogs": ["d1", "d2", "d3"]},
                            unfiled=["x1"])

    r = reconcile.reconcile(conn, same, sync_id="sync-2", live_graph_path=graph_path)

    assert r.events == []
    assert conn.execute("SELECT id FROM topics WHERE name = 'Cats'").fetchone()["id"] == cats_uuid
    assert conn.execute("SELECT version FROM topics WHERE name = 'Cats'").fetchone()["version"] == 1
    assert conn.execute(
        "SELECT COUNT(*) c FROM topic_events WHERE sync_id = 'sync-2'"
    ).fetchone()["c"] == 0


def test_dropped_cluster_is_tombstoned_not_deleted(conn, graph_path, base_corpus, after_first_sync):
    dogs_uuid = after_first_sync["Dogs"]
    # Dogs chunks vanish from the corpus; only Cats remains
    shrunk = {k: v for k, v in base_corpus.items() if not k.startswith("d")}
    conn.execute("DELETE FROM chunks WHERE chunk_id LIKE 'd%'")
    conn.execute("DELETE FROM chunk_vectors WHERE chunk_id LIKE 'd%'")
    conn.commit()
    result = _organize_result(shrunk, {"Cats": ["c1", "c2", "c3"]}, unfiled=["x1"])

    r = reconcile.reconcile(conn, result, sync_id="sync-2", live_graph_path=graph_path)

    assert "tombstone" in r.event_counts
    row = conn.execute("SELECT tombstoned FROM topics WHERE id = ?", (dogs_uuid,)).fetchone()
    assert row is not None and row["tombstoned"] == 1          # kept, flagged
    assert conn.execute(
        "SELECT event_type FROM topic_events WHERE topic_id = ? AND sync_id = 'sync-2'",
        (dogs_uuid,),
    ).fetchone()["event_type"] == "tombstone"


def test_split_carries_uuid_to_the_dominant_child(conn, graph_path, base_corpus, after_first_sync):
    cats_uuid = after_first_sync["Cats"]
    # Cats splits: c1+c2 stay "Cats", c3 breaks off as "Kittens"
    result = _organize_result(
        base_corpus,
        {"Cats": ["c1", "c2"], "Kittens": ["c3"], "Dogs": ["d1", "d2", "d3"]},
        unfiled=["x1"],
    )

    r = reconcile.reconcile(conn, result, sync_id="sync-2", live_graph_path=graph_path)

    assert r.event_counts.get("split") == 1
    assert conn.execute("SELECT id FROM topics WHERE name = 'Cats'").fetchone()["id"] == cats_uuid
    # Kittens is a brand-new topic id
    kittens = conn.execute("SELECT id, version FROM topics WHERE name = 'Kittens'").fetchone()
    assert kittens["id"] != cats_uuid and kittens["version"] == 1
    # Cats version bumped (it materially changed)
    assert conn.execute("SELECT version FROM topics WHERE id = ?", (cats_uuid,)).fetchone()["version"] == 2


def test_nested_tree_persists_with_correct_levels_and_parents(conn, graph_path, base_corpus):
    _seed_chunks(conn, base_corpus)
    # a real 2-level tree: "Science" over "Biology"; "Coffee" stands alone
    nodes = [
        ClusterNode(key="p-sci", level=0, label="Science", description="d",
                    centroid=_centroid(base_corpus, ["d1", "d2", "d3"]),
                    member_chunk_ids=["d1", "d2", "d3"], direct_chunk_ids=[],
                    parent_key=None, child_keys=["b-bio"]),
        ClusterNode(key="b-bio", level=1, label="Biology", description="d",
                    centroid=_centroid(base_corpus, ["d1", "d2", "d3"]),
                    member_chunk_ids=["d1", "d2", "d3"], direct_chunk_ids=["d1", "d2", "d3"],
                    parent_key="p-sci", child_keys=[]),
        ClusterNode(key="b-cof", level=0, label="Coffee", description="d",
                    centroid=_centroid(base_corpus, ["c1", "c2", "c3"]),
                    member_chunk_ids=["c1", "c2", "c3"], direct_chunk_ids=["c1", "c2", "c3"],
                    parent_key=None, child_keys=[]),
    ]
    result = OrganizeResult(
        nodes=nodes, root_keys=["p-sci", "b-cof"], unfiled=ClusterNode(
            key=config.UNFILED_TOPIC_ID, level=0, label="Unfiled", description="",
            centroid=_centroid(base_corpus, ["x1"]), member_chunk_ids=["x1"], direct_chunk_ids=["x1"]),
        unfiled_topic_id=config.UNFILED_TOPIC_ID, outlier_scores={}, unfiled_pct=0.0,
        total_chunks=7, used_kmeans_fallback=False,
    )

    reconcile.reconcile(conn, result, sync_id="sync-1", live_graph_path=graph_path)

    levels = {row["name"]: row["level"]
              for row in conn.execute("SELECT name, level FROM topics").fetchall()}
    assert levels[config.ROOT_TOPIC_NAME] == 0
    assert levels["Science"] == 1 and levels["Coffee"] == 1
    assert levels["Biology"] == 2
    # Biology's cached ancestor_path is root -> Science -> Biology
    bio_path = conn.execute(
        "SELECT ancestor_path FROM topics WHERE name = 'Biology'"
    ).fetchone()["ancestor_path"]
    assert bio_path is not None and bio_path.count('"') == 6      # three ids


def test_rename_only_fires_when_a_materially_changed_node_gets_a_new_label(
    conn, graph_path, base_corpus, after_first_sync
):
    cats_uuid = after_first_sync["Cats"]
    # same members, new label, no drift -> re-label discipline keeps the old name, no rename
    steady = _organize_result(
        base_corpus, {"Felines": ["c1", "c2", "c3"], "Dogs": ["d1", "d2", "d3"]}, unfiled=["x1"],
    )
    r = reconcile.reconcile(conn, steady, sync_id="sync-2", live_graph_path=graph_path)
    assert "rename" not in r.event_counts
    assert conn.execute("SELECT name FROM topics WHERE id = ?", (cats_uuid,)).fetchone()["name"] == "Cats"
