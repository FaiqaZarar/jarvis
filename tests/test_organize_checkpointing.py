"""Organize checkpointing + config versioning (docs/organize-checkpointing-DESIGN.md).

Fast, in-process coverage: config version creation/lookup, per-cluster
checkpoint fingerprint matching (a real hit and a deliberate miss), and
the three resume-decision scenarios from the design doc's §4. The real
interrupt-and-resume test (a genuine subprocess kill, not a mocked one)
lives in test_organize_checkpointing_interrupt.py, marked slow like the
cross-process determinism test it reuses the methodology from.
"""

import json

import pytest

from jarvis_server.internal.migrate import run_migrations
from jarvis_server.internal import db
from jarvis_server import pipeline
from jarvis_server.connectors.filesystem import FilesystemConnector
from jarvis_server.organize import clustering, config, config_versions
from jarvis_server.pipeline import run_ingest
from tests.test_clustering import CyclingLLM, StubEmbedder, _blobs, _seed_chunks
from tests.test_pipeline import CyclingLLM as PipelineCyclingLLM
from tests.test_pipeline import TopicEmbedder
from tests.test_pipeline import topical_corpus  # noqa: F401 - reused as a fixture


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "checkpointing.db"
    run_migrations(path)
    c = db.get_connection(path)
    yield c
    c.close()


# ---------------------------------------------------------------------
# Config versions
# ---------------------------------------------------------------------

def test_ensure_config_version_bootstraps_exactly_once(conn):
    assert db.get_current_config_version(conn) is None

    v1 = config_versions.ensure_config_version(conn)
    assert db.get_current_config_version(conn)["id"] == v1

    v2 = config_versions.ensure_config_version(conn)
    assert v2 == v1, "a second call must reuse the bootstrapped version, not insert another"


def test_record_new_config_version_always_inserts_a_new_row(conn):
    """Deliberate creation, unlike ensure_config_version - always a new
    row, even with byte-identical params (design doc: a version changes
    only when someone deliberately says so, never by diffing values)."""
    v1 = config_versions.record_new_config_version(conn, note="first")
    v2 = config_versions.record_new_config_version(conn, note="second, unchanged params")

    assert v2 != v1
    assert db.get_current_config_version(conn)["id"] == v2


def test_current_config_version_is_the_most_recently_inserted(conn):
    config_versions.record_new_config_version(conn)
    config_versions.record_new_config_version(conn)
    v3 = config_versions.record_new_config_version(conn)

    assert db.get_current_config_version(conn)["id"] == v3


def test_recorded_params_json_matches_config_snapshot(conn):
    version_id = config_versions.record_new_config_version(conn)
    row = db.get_current_config_version(conn)

    assert row["id"] == version_id
    assert json.loads(row["params_json"]) == config.snapshot()


# ---------------------------------------------------------------------
# Per-cluster checkpoint fingerprint matching
# ---------------------------------------------------------------------

def test_checkpoint_hit_skips_the_llm_call_and_reuses_the_stored_label(conn):
    """Run organize() once (real labeling), confirm it checkpointed a
    base cluster; run it again under the SAME run_id with a fresh LLM -
    the fingerprint must hit, so the second run makes zero LLM calls and
    reuses the exact label from the first."""
    run_id = "run-hit"
    matrix, _truth = _blobs(n_clusters=2, per_cluster=6, seed=42)
    _seed_chunks(conn, matrix)

    llm1 = CyclingLLM()
    result1 = clustering.organize(conn, StubEmbedder(), llm1, run_id=run_id)
    base_nodes_1 = [n for n in result1.nodes if n.key.startswith("base-")]
    assert len(base_nodes_1) >= 1
    assert len(llm1.calls) >= len(base_nodes_1), "first run must have actually called the LLM"

    target = base_nodes_1[0]
    fingerprint = clustering._fingerprint_members(target.member_chunk_ids)
    checkpoint = db.get_cluster_checkpoint(conn, run_id, fingerprint)
    assert checkpoint is not None, "first run must have persisted a checkpoint for this cluster"
    assert checkpoint["label"] == target.label

    llm2 = CyclingLLM()
    result2 = clustering.organize(conn, StubEmbedder(), llm2, run_id=run_id)

    assert len(llm2.calls) == 0, "every base cluster fingerprint should have hit - no LLM calls at all"
    base_nodes_2 = {n.key: n for n in result2.nodes if n.key.startswith("base-")}
    assert base_nodes_2[target.key].label == target.label
    assert base_nodes_2[target.key].description == target.description


def test_checkpoint_miss_labels_fresh_and_never_misattaches_a_stored_label(conn):
    """A checkpoint row exists for this run_id, but under a fingerprint
    that doesn't match anything organize() actually produces (simulating
    clustering coming out even slightly different). It must never be
    picked up - the real cluster gets a fresh LLM call and its own,
    correct checkpoint."""
    run_id = "run-miss"
    matrix, _truth = _blobs(n_clusters=2, per_cluster=6, seed=43)
    _seed_chunks(conn, matrix)

    bogus_fingerprint = clustering._fingerprint_members(["not-a-real-chunk-id-1", "not-a-real-chunk-id-2"])
    db.insert_cluster_checkpoint(
        conn, run_id, bogus_fingerprint,
        "WRONG LABEL SHOULD NEVER APPEAR", "wrong description", "2020-01-01T00:00:00+00:00",
    )

    llm = CyclingLLM()
    result = clustering.organize(conn, StubEmbedder(), llm, run_id=run_id)
    base_nodes = [n for n in result.nodes if n.key.startswith("base-")]

    assert len(base_nodes) >= 1
    assert all(n.label != "WRONG LABEL SHOULD NEVER APPEAR" for n in base_nodes), (
        "the bogus checkpoint must never be attached to real content"
    )
    assert len(llm.calls) >= len(base_nodes), "every real cluster must have gotten a fresh LLM call"

    for n in base_nodes:
        real_fingerprint = clustering._fingerprint_members(n.member_chunk_ids)
        checkpoint = db.get_cluster_checkpoint(conn, run_id, real_fingerprint)
        assert checkpoint is not None
        assert checkpoint["label"] == n.label


# ---------------------------------------------------------------------
# Parent roll-up checkpointing - the same mechanism as above, applied to
# _build_parent_levels' per-group _label_parent calls (previously
# un-checkpointed - see the design doc §1b/§5 and _build_parent_levels'
# own docstring for why per-group checkpointing is safe despite the
# roll-up's same-round sibling-dedup ordering dependency).
# ---------------------------------------------------------------------

def test_rollup_checkpoint_hit_skips_the_llm_call_and_reuses_the_stored_label(conn, monkeypatch):
    """Run _build_parent_levels once (real labeling) over a base list that
    the pairing stub is guaranteed to roll up; run it again under the
    SAME run_id, over a freshly-built (unmutated) copy of the same base
    nodes, with a fresh LLM - every parent group's fingerprint must hit,
    so the second pass makes zero LLM calls and reproduces the exact same
    tree (labels, descriptions, roots) as the first."""
    from tests.test_clustering import _pairing_base_cluster

    run_id = "rollup-run-hit"
    monkeypatch.setattr(clustering, "_base_cluster", _pairing_base_cluster)

    def _fresh_base():
        return [
            clustering.ClusterNode(
                key=f"base-{i}", level=-1, label=f"L{i}", description="d",
                centroid=[0.0] * 4, member_chunk_ids=[f"c{i}"],
            )
            for i in range(30)
        ]

    llm1 = CyclingLLM()
    parents1, roots1 = clustering._build_parent_levels(
        _fresh_base(), StubEmbedder(), llm1, checkpoint_conn=conn, run_id=run_id,
    )
    assert parents1, "the pairing base_cluster stub must actually roll something up"
    assert len(llm1.calls) == len(parents1), "first pass must call the LLM once per parent group"

    llm2 = CyclingLLM()
    parents2, roots2 = clustering._build_parent_levels(
        _fresh_base(), StubEmbedder(), llm2, checkpoint_conn=conn, run_id=run_id,
    )

    assert len(llm2.calls) == 0, "every parent-group fingerprint should have hit - no LLM calls at all"
    assert [p.label for p in parents2] == [p.label for p in parents1]
    assert [p.description for p in parents2] == [p.description for p in parents1]
    assert roots2 == roots1


def test_rollup_checkpoint_miss_labels_fresh_and_never_misattaches_a_stored_label(conn, monkeypatch):
    """A checkpoint row exists for this run_id under a fingerprint that
    doesn't match any real group's membership - it must never be picked
    up; every real group still gets a fresh LLM call and its own,
    correct checkpoint."""
    from tests.test_clustering import _pairing_base_cluster

    run_id = "rollup-run-miss"
    monkeypatch.setattr(clustering, "_base_cluster", _pairing_base_cluster)

    bogus_fingerprint = clustering._fingerprint_members(["not-a-real-chunk-id-1", "not-a-real-chunk-id-2"])
    db.insert_cluster_checkpoint(
        conn, run_id, bogus_fingerprint,
        "WRONG LABEL SHOULD NEVER APPEAR", "wrong description", "2020-01-01T00:00:00+00:00",
    )

    base = [
        clustering.ClusterNode(
            key=f"base-{i}", level=-1, label=f"L{i}", description="d",
            centroid=[0.0] * 4, member_chunk_ids=[f"c{i}"],
        )
        for i in range(30)
    ]
    llm = CyclingLLM()
    parents, _roots = clustering._build_parent_levels(
        base, StubEmbedder(), llm, checkpoint_conn=conn, run_id=run_id,
    )

    assert parents
    assert all(p.label != "WRONG LABEL SHOULD NEVER APPEAR" for p in parents)
    assert len(llm.calls) == len(parents), "every real group must have gotten a fresh LLM call"


# ---------------------------------------------------------------------
# Resume decision (design doc §4) - the three scenarios
# ---------------------------------------------------------------------

def test_resume_scenario_same_config_version_reuses_the_in_progress_run(conn):
    v1 = config_versions.ensure_config_version(conn)
    db.insert_organize_run(conn, "run-abc", v1, "2020-01-01T00:00:00+00:00")

    run_id, version_id, is_resume = pipeline._resolve_organize_run(conn)

    assert (run_id, version_id, is_resume) == ("run-abc", v1, True)


def test_resume_scenario_different_config_version_and_broken_restarts_from_scratch(conn):
    v_old = config_versions.record_new_config_version(conn, note="old")
    db.insert_organize_run(conn, "run-old-broken", v_old, "2020-01-01T00:00:00+00:00")
    v_new = config_versions.record_new_config_version(conn, note="deliberately changed a tunable")

    run_id, version_id, is_resume = pipeline._resolve_organize_run(conn)

    assert is_resume is False
    assert run_id != "run-old-broken"
    assert version_id == v_new

    # The old, superseded run is left behind untouched - not deleted,
    # not mutated (design doc §4/§5 - retention is a separate, still-
    # open question, but "leave it alone" is the one thing that IS decided).
    old = conn.execute("SELECT * FROM organize_runs WHERE id = ?", ("run-old-broken",)).fetchone()
    assert old["status"] == "in_progress"
    assert old["config_version_id"] == v_old


def test_resume_scenario_completed_under_old_version_does_not_force_a_rerun(tmp_path, topical_corpus):
    """The third scenario isn't really about _resolve_organize_run at all
    - a completed run is never 'in_progress', so it never reaches that
    function again. What actually has to be true is that a config-version
    bump alone, with no live-chunk-set change, never trips
    _hierarchy_rebuild_reason - the one real gate that decides whether
    anything reruns at all. Goes through the real run_ingest() over
    test_pipeline.py's own multi-topic corpus (not hand-seeded chunks),
    so both `_live_embedded_chunk_ids`'s document_sources-based liveness
    check and real topic birth are exercised for real."""
    db_path = tmp_path / "resume_scenario.db"
    run_migrations(db_path)
    conn = db.get_connection(db_path)
    graph_path = tmp_path / "resume_scenario_graph.lbdb"

    first = run_ingest(
        conn, FilesystemConnector(str(topical_corpus)),
        embedder=TopicEmbedder(), llm_client=PipelineCyclingLLM(), live_graph_path=graph_path,
    )
    assert "organize" in first["hierarchy"]

    run_id = first["hierarchy"]["sync_id"]
    assert db.get_current_config_version(conn) is not None, "a real organize run must have recorded a version"

    # Confirm the baseline first: right after a real completed sync, with
    # nothing new ingested, there is genuinely nothing to rebuild.
    assert pipeline._hierarchy_rebuild_reason(conn) is None
    run_row = conn.execute("SELECT status FROM organize_runs WHERE id = ?", (run_id,)).fetchone()
    assert run_row["status"] == "completed"

    # Now bump the config version - per the design doc, this must NOT by
    # itself resurrect a rebuild reason. Only a live-chunk-set change does.
    config_versions.record_new_config_version(conn, note="deliberately changed after the sync completed")

    assert pipeline._hierarchy_rebuild_reason(conn) is None, (
        "a config-version change alone must never force a rerun of an "
        "already-completed hierarchy - only a real live-chunk-set change should"
    )

    # And a second run_ingest() call (the real, end-to-end path) must
    # still report a no-op, exactly like it would have before any of
    # this feature existed.
    second = run_ingest(
        conn, FilesystemConnector(str(topical_corpus)),
        embedder=TopicEmbedder(), llm_client=PipelineCyclingLLM(), live_graph_path=graph_path,
    )
    assert second["hierarchy"] == {"skipped": "live chunk set unchanged (no-op re-sync)"}

    conn.close()
