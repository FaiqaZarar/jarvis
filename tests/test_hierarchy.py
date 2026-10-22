"""J-10 acceptance checks: get_hierarchy / browse / related.

Same approach as test_search.py - seed topics / chunks / chunk_vectors /
the cache columns straight into SQLite, no LadybugDB, no network. The
three tools are pure SQLite readers, so the internal `_*_impl` functions
are called directly.
"""

import json
import struct
from pathlib import Path

import pytest

from jarvis_server.internal import db
from jarvis_server.internal.embed.embed import _vector_to_blob
from jarvis_server.internal.migrate import run_migrations
from jarvis_server.mcp_tools.hierarchy import (
    _browse_impl,
    _get_hierarchy_impl,
    _related_impl,
)
from jarvis_server.mcp_tools.models import (
    BrowseResult,
    ErrorResponse,
    HierarchyResult,
    RelatedResult,
)
from jarvis_server.organize.config import ROOT_TOPIC_ID, UNFILED_TOPIC_ID

A_ID = "aaaaaaaa-0000-4000-8000-000000000001"
A1_ID = "aaaaaaaa-0000-4000-8000-000000000002"
B_ID = "bbbbbbbb-0000-4000-8000-000000000001"
T_ID = "cccccccc-0000-4000-8000-000000000001"   # tombstoned, superseded via column
T2_ID = "cccccccc-0000-4000-8000-000000000002"  # tombstoned, superseded via event log

# lineage_tree ids: Science > {Biology > Genetics, Chemistry > Biochemistry}; History
SCI_ID = "5c1e0000-0000-4000-8000-000000000001"
BIO_ID = "5c1e0000-0000-4000-8000-000000000002"
GEN_ID = "5c1e0000-0000-4000-8000-000000000003"
CHEM_ID = "5c1e0000-0000-4000-8000-000000000004"
BCHEM_ID = "5c1e0000-0000-4000-8000-000000000005"
HIST_ID = "5c1e0000-0000-4000-8000-000000000006"


def _vec(*head: float) -> list[float]:
    v = list(head) + [0.0] * (1024 - len(head))
    return v[:1024]


@pytest.fixture
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    connection = db.get_connection(db_path)
    yield connection
    connection.close()


def _seed_topic(
    conn, topic_id, name, *, level=1, description="desc", centroid=None,
    reserved=0, tombstoned=0, superseded_by=None, children_ids=None,
    ancestor_path=None, direct_chunk_count=0, subtree_chunk_count=0,
):
    conn.execute(
        """
        INSERT INTO topics
            (id, name, description, centroid, level, version, reserved, tombstoned,
             superseded_by, children_ids, ancestor_path, direct_chunk_count, subtree_chunk_count)
        VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            topic_id, name, description,
            _vector_to_blob(centroid) if centroid is not None else None,
            level, reserved, tombstoned, superseded_by,
            json.dumps(children_ids) if children_ids is not None else None,
            json.dumps(ancestor_path) if ancestor_path is not None else None,
            direct_chunk_count, subtree_chunk_count,
        ),
    )
    conn.commit()


def _seed_source(conn, content_hash, connector="filesystem", source_id=None, title="doc.md"):
    source_id = source_id or f"/tmp/{content_hash}.md"
    conn.execute(
        "INSERT INTO documents (content_hash, normalized_markdown, first_seen_at, last_ingested_at) "
        "VALUES (?, '# T', 'now', 'now') ON CONFLICT DO NOTHING",
        (content_hash,),
    )
    db.insert_document_source(
        conn, source_id=source_id, connector=connector, content_hash=content_hash,
        source_last_seen="now", source_modified_at="2026-01-01T00:00:00Z", mime=None, title=title,
    )


def _seed_chunk(conn, chunk_id, content_hash, text, *, topic_id=None, confidence=None, vector=None):
    conn.execute(
        "INSERT INTO chunks (chunk_id, content_hash, ordinal, chunk_hash, text, topic_id, topic_confidence) "
        "VALUES (?, ?, 0, ?, ?, ?, ?)",
        (chunk_id, content_hash, chunk_id, text, topic_id, confidence),
    )
    if vector is not None:
        conn.execute(
            "INSERT INTO chunk_vectors (chunk_id, embedding) VALUES (?, ?)",
            (chunk_id, _vector_to_blob(vector)),
        )
    conn.commit()


def _set_sync_id(conn, sync_id):
    conn.execute(
        "INSERT INTO sync_state (id, current_sync_id) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET current_sync_id = excluded.current_sync_id",
        (sync_id,),
    )
    conn.commit()


@pytest.fixture
def tree(conn):
    """ROOT(Knowledge Base) ├─ Photography ─ Aperture   ├─ Finance   └─ Unfiled
    plus two tombstoned topics. Chunks: Aperture has 2 direct + Photography
    has 1 direct (so a subtree chunk exists), Finance has 3 (for pagination)."""
    _seed_topic(conn, ROOT_TOPIC_ID, "Knowledge Base", level=0, reserved=1,
                children_ids=[A_ID, B_ID, UNFILED_TOPIC_ID], ancestor_path=[ROOT_TOPIC_ID],
                centroid=None, subtree_chunk_count=6)
    _seed_topic(conn, A_ID, "Photography", level=1, centroid=_vec(1.0, 0.0, 0.0),
                children_ids=[A1_ID], ancestor_path=[ROOT_TOPIC_ID, A_ID],
                direct_chunk_count=1, subtree_chunk_count=3)
    _seed_topic(conn, A1_ID, "Aperture", level=2, centroid=_vec(0.9, 0.1, 0.0),
                children_ids=[], ancestor_path=[ROOT_TOPIC_ID, A_ID, A1_ID],
                direct_chunk_count=2, subtree_chunk_count=2)
    _seed_topic(conn, B_ID, "Finance", level=1, centroid=_vec(0.0, 1.0, 0.0),
                children_ids=[], ancestor_path=[ROOT_TOPIC_ID, B_ID],
                direct_chunk_count=3, subtree_chunk_count=3)
    _seed_topic(conn, UNFILED_TOPIC_ID, "Unfiled", level=1, reserved=1,
                centroid=_vec(0.5, 0.5, 0.0), children_ids=[],
                ancestor_path=[ROOT_TOPIC_ID, UNFILED_TOPIC_ID], direct_chunk_count=0)
    _seed_topic(conn, T_ID, "Old Photography", level=1, tombstoned=1, superseded_by=A_ID,
                centroid=_vec(0.85, 0.15, 0.0), ancestor_path=[])
    _seed_topic(conn, T2_ID, "Merged Away", level=1, tombstoned=1, superseded_by=None,
                centroid=None, ancestor_path=[])
    conn.execute(
        "INSERT INTO topic_events (topic_id, sync_id, event_type, detail_json, created_at) "
        "VALUES (?, 's1', 'tombstone', ?, 'now')",
        (T2_ID, json.dumps({"merged_into": B_ID})),
    )

    # chunks
    _seed_source(conn, "doc_ap", source_id="/tmp/aperture.md")
    _seed_source(conn, "doc_fin", source_id="/tmp/finance.md")
    _seed_source(conn, "doc_photo", source_id="/tmp/photo.md")
    _seed_chunk(conn, "c_ap_1", "doc_ap", "aperture and f-number", topic_id=A1_ID,
                confidence=0.94, vector=_vec(1.0, 0.0, 0.0))
    _seed_chunk(conn, "c_ap_2", "doc_ap", "depth of field blur", topic_id=A1_ID,
                confidence=0.90, vector=_vec(0.95, 0.05, 0.0))       # same doc as c_ap_1
    _seed_chunk(conn, "c_photo_1", "doc_photo", "composition rule of thirds", topic_id=A_ID,
                confidence=0.7, vector=_vec(0.8, 0.2, 0.0))          # direct member of Photography
    _seed_chunk(conn, "c_fin_1", "doc_fin", "emergency fund basics", topic_id=B_ID,
                confidence=0.8, vector=_vec(0.0, 1.0, 0.0))
    _seed_chunk(conn, "c_fin_2", "doc_fin", "high yield savings", topic_id=B_ID,
                confidence=0.8, vector=_vec(0.0, 0.95, 0.05))
    _seed_chunk(conn, "c_fin_3", "doc_fin", "automating transfers", topic_id=B_ID,
                confidence=0.8, vector=_vec(0.0, 0.9, 0.1))
    _set_sync_id(conn, "sync-1")
    return conn


@pytest.fixture
def lineage_tree(conn):
    """A 4-level branch so related(topic) has real ancestors, descendants,
    siblings, and cousins to sort out (issue #50):

        Knowledge Base
          Science
            Biology
              Genetics
            Chemistry
              Biochemistry
          History
    """
    _seed_topic(conn, ROOT_TOPIC_ID, "Knowledge Base", level=0, reserved=1,
                ancestor_path=[ROOT_TOPIC_ID])
    _seed_topic(conn, SCI_ID, "Science", level=1, centroid=_vec(1.0, 0.0, 0.0),
                ancestor_path=[ROOT_TOPIC_ID, SCI_ID])
    _seed_topic(conn, BIO_ID, "Biology", level=2, centroid=_vec(0.9, 0.3, 0.0),
                ancestor_path=[ROOT_TOPIC_ID, SCI_ID, BIO_ID])
    _seed_topic(conn, GEN_ID, "Genetics", level=3, centroid=_vec(0.88, 0.34, 0.0),
                ancestor_path=[ROOT_TOPIC_ID, SCI_ID, BIO_ID, GEN_ID])
    _seed_topic(conn, CHEM_ID, "Chemistry", level=2, centroid=_vec(0.85, 0.0, 0.4),
                ancestor_path=[ROOT_TOPIC_ID, SCI_ID, CHEM_ID])
    _seed_topic(conn, BCHEM_ID, "Biochemistry", level=3, centroid=_vec(0.8, 0.3, 0.35),
                ancestor_path=[ROOT_TOPIC_ID, SCI_ID, CHEM_ID, BCHEM_ID])
    _seed_topic(conn, HIST_ID, "History", level=1, centroid=_vec(0.0, 1.0, 0.0),
                ancestor_path=[ROOT_TOPIC_ID, HIST_ID])
    _set_sync_id(conn, "sync-1")
    return conn


# ---------------------------------------------------------------------
# get_hierarchy
# ---------------------------------------------------------------------

def test_get_hierarchy_hides_synthetic_root_and_returns_forest(tree):
    result = _get_hierarchy_impl(tree, None, None)
    assert isinstance(result, HierarchyResult)
    names = [t.name for t in result.topics]
    assert names == ["Finance", "Photography", "Unfiled"]   # ordered by name, root hidden
    assert "Knowledge Base" not in names


def test_get_hierarchy_unfiled_present_and_flagged_reserved(tree):
    result = _get_hierarchy_impl(tree, None, None)
    unfiled = next(t for t in result.topics if t.name == "Unfiled")
    assert unfiled.reserved is True
    photography = next(t for t in result.topics if t.name == "Photography")
    assert photography.reserved is False


def test_get_hierarchy_breadcrumb_excludes_synthetic_root(tree):
    result = _get_hierarchy_impl(tree, None, None)
    photography = next(t for t in result.topics if t.name == "Photography")
    aperture = photography.children[0]
    assert photography.path == "Photography"
    assert aperture.path == "Photography > Aperture"


def test_get_hierarchy_full_recursion_when_depth_omitted(tree):
    result = _get_hierarchy_impl(tree, None, None)
    photography = next(t for t in result.topics if t.name == "Photography")
    assert [c.name for c in photography.children] == ["Aperture"]
    assert photography.children[0].children == []          # expanded, genuinely no kids


def test_get_hierarchy_depth_zero_is_just_the_node(tree):
    result = _get_hierarchy_impl(tree, A_ID, 0)
    assert isinstance(result, HierarchyResult)
    assert len(result.topics) == 1
    assert result.topics[0].id == A_ID
    assert result.topics[0].children is None               # not expanded


def test_get_hierarchy_depth_one_is_node_plus_direct_children(tree):
    result = _get_hierarchy_impl(tree, A_ID, 1)
    node = result.topics[0]
    assert [c.name for c in node.children] == ["Aperture"]
    assert node.children[0].children is None               # grandchildren truncated


def test_get_hierarchy_explicit_root_returns_that_subtree(tree):
    result = _get_hierarchy_impl(tree, A_ID, None)
    assert [t.id for t in result.topics] == [A_ID]
    assert result.topics[0].children[0].id == A1_ID


def test_get_hierarchy_unknown_root_is_not_found(tree):
    result = _get_hierarchy_impl(tree, "no-such-id", None)
    assert isinstance(result, ErrorResponse)
    assert result.error.code == "not_found"


def test_get_hierarchy_tombstoned_root_returns_node_flagged(tree):
    result = _get_hierarchy_impl(tree, T_ID, None)
    assert isinstance(result, HierarchyResult)
    node = result.topics[0]
    assert node.tombstoned is True
    assert node.superseded_by == A_ID


def test_get_hierarchy_negative_depth_is_invalid_argument(tree):
    result = _get_hierarchy_impl(tree, None, -1)
    assert isinstance(result, ErrorResponse)
    assert result.error.code == "invalid_argument"


def test_get_hierarchy_empty_store_returns_empty_forest(conn):
    result = _get_hierarchy_impl(conn, None, None)
    assert isinstance(result, HierarchyResult)
    assert result.topics == []


def test_get_hierarchy_low_confidence_is_corpus_wide_not_scoped_to_root_or_depth(tree):
    """low_confidence / low_confidence_reason come from organize_state (the
    last organize run's whole-corpus assessment) and must be identical on
    every response regardless of which subtree or depth was asked for.
    See docs/LOW_CONFIDENCE_DESIGN.md."""
    # nothing written yet -> honest default
    assert _get_hierarchy_impl(tree, None, None).low_confidence is False
    assert _get_hierarchy_impl(tree, None, None).low_confidence_reason is None

    reason = "only 6 embedded chunk(s) - below the ~15; treated as provisional"
    db.set_organize_state(tree, True, reason, "2026-09-03T00:00:00+00:00")

    for root, depth in [(None, None), (None, 1), (A_ID, 0), (A_ID, 1), (A_ID, None), (B_ID, None)]:
        result = _get_hierarchy_impl(tree, root, depth)
        assert isinstance(result, HierarchyResult)
        assert result.low_confidence is True
        assert result.low_confidence_reason == reason


# ---------------------------------------------------------------------
# browse
# ---------------------------------------------------------------------

def test_browse_returns_direct_members_only_not_subtree(tree):
    result = _browse_impl(tree, A_ID, None, None)
    assert isinstance(result, BrowseResult)
    chunk_ids = {c.chunk_id for c in result.chunks}
    assert chunk_ids == {"c_photo_1"}                       # direct member of Photography
    assert "c_ap_1" not in chunk_ids and "c_ap_2" not in chunk_ids   # those are subtree


def test_browse_includes_full_child_topic_nodes(tree):
    result = _browse_impl(tree, A_ID, None, None)
    assert [c.name for c in result.topic.children] == ["Aperture"]
    assert result.topic.children[0].direct_chunk_count == 2


def test_browse_direct_chunks_are_full_chunk_hits_with_topic(tree):
    result = _browse_impl(tree, A1_ID, None, None)
    hit = next(c for c in result.chunks if c.chunk_id == "c_ap_1")
    assert hit.text == "aperture and f-number"
    assert hit.topic is not None and hit.topic.id == A1_ID
    assert hit.topic.path == "Photography > Aperture"
    assert hit.confidence == pytest.approx(0.94)
    assert hit.score is None                                # browse is not a ranking


def test_browse_paginates_direct_chunks(tree):
    first = _browse_impl(tree, B_ID, None, 2)
    assert isinstance(first, BrowseResult)
    assert len(first.chunks) == 2
    assert first.next_cursor is not None

    second = _browse_impl(tree, B_ID, first.next_cursor, 2)
    assert len(second.chunks) == 1
    assert second.next_cursor is None
    assert (
        {c.chunk_id for c in first.chunks} | {c.chunk_id for c in second.chunks}
        == {"c_fin_1", "c_fin_2", "c_fin_3"}
    )


def test_browse_stale_cursor_after_resync(tree):
    first = _browse_impl(tree, B_ID, None, 2)
    _set_sync_id(tree, "sync-2")                            # a re-sync happened
    result = _browse_impl(tree, B_ID, first.next_cursor, 2)
    assert isinstance(result, ErrorResponse)
    assert result.error.code == "stale_cursor"


def test_browse_unknown_id_is_not_found(tree):
    result = _browse_impl(tree, "no-such-id", None, None)
    assert isinstance(result, ErrorResponse)
    assert result.error.code == "not_found"


def test_browse_malformed_cursor_is_invalid_argument(tree):
    result = _browse_impl(tree, B_ID, "!!!not-base64!!!", None)
    assert isinstance(result, ErrorResponse)
    assert result.error.code == "invalid_argument"


# ---------------------------------------------------------------------
# related - chunk -> chunks
# ---------------------------------------------------------------------

def test_related_chunk_excludes_self_crosses_topics_keeps_same_document(tree):
    result = _related_impl(tree, "c_ap_1", None)
    assert isinstance(result, RelatedResult)
    assert result.chunks is not None and result.topics is None
    ids = [c.chunk_id for c in result.chunks]

    assert "c_ap_1" not in ids                              # self excluded
    assert ids[0] == "c_ap_2"                               # same document, nearest
    assert "c_photo_1" in ids                               # different topic, still returned
    # similarity present and descending
    sims = [c.similarity for c in result.chunks]
    assert all(s is not None for s in sims)
    assert sims == sorted(sims, reverse=True)
    assert result.chunks[0].score is None


def test_related_chunk_respects_limit(tree):
    result = _related_impl(tree, "c_ap_1", 2)
    assert len(result.chunks) == 2


def test_related_unknown_chunk_is_not_found(tree):
    result = _related_impl(tree, "f" * 64, None)            # 64-hex -> dispatched as a chunk id
    assert isinstance(result, ErrorResponse)
    assert result.error.code == "not_found"


def test_related_vanished_chunk_is_not_found(tree):
    tree.execute("DELETE FROM chunks WHERE chunk_id = 'c_fin_1'")
    tree.commit()
    result = _related_impl(tree, "c_fin_1", None)
    assert isinstance(result, ErrorResponse)
    assert result.error.code == "not_found"


def test_related_chunk_excludes_tombstoned_source_hits(tree):
    tree.execute("UPDATE document_sources SET tombstoned = 1 WHERE content_hash = 'doc_fin'")
    tree.commit()
    result = _related_impl(tree, "c_ap_1", None)
    ids = [c.chunk_id for c in result.chunks]
    assert not any(i.startswith("c_fin") for i in ids)


# ---------------------------------------------------------------------
# related - topic -> topics
# ---------------------------------------------------------------------

def test_related_topic_ranks_by_centroid_similarity(lineage_tree):
    result = _related_impl(lineage_tree, BIO_ID, None)
    assert isinstance(result, RelatedResult)
    assert result.topics is not None and result.chunks is None
    names = [t.name for t in result.topics]
    assert names[0] == "Biochemistry"                       # nearest kept centroid to Biology
    assert "History" in names
    sims = [t.similarity for t in result.topics]
    assert sims == sorted(sims, reverse=True)
    assert all(0.0 <= s <= 1.0001 for s in sims)


def test_related_topic_excludes_ancestors_and_descendants(lineage_tree):
    """issue #50: a topic's parent and child must not appear in its
    related() results even though their centroids are the closest of all."""
    result = _related_impl(lineage_tree, BIO_ID, None)
    returned = {t.id for t in result.topics}

    assert SCI_ID not in returned      # ancestor (parent)
    assert GEN_ID not in returned      # descendant (child)
    assert BIO_ID not in returned      # the target itself


def test_related_topic_keeps_siblings_and_cousins(lineage_tree):
    """The structural exclusion is lineage-only: a sibling and a cousin
    (neither on the target's ancestor_path, nor the target on theirs) stay."""
    result = _related_impl(lineage_tree, BIO_ID, None)
    returned = {t.id for t in result.topics}

    assert CHEM_ID in returned         # sibling (both children of Science)
    assert BCHEM_ID in returned        # cousin (Science > Chemistry > Biochemistry)
    assert HIST_ID in returned         # unrelated top-level topic

    # from Genetics, its parent Biology and grandparent Science drop out,
    # but the cousin branch is still reachable.
    gen_returned = {t.id for t in _related_impl(lineage_tree, GEN_ID, None).topics}
    assert BIO_ID not in gen_returned and SCI_ID not in gen_returned
    assert BCHEM_ID in gen_returned and CHEM_ID in gen_returned


def test_related_topic_excludes_unfiled_tombstoned_and_root(tree):
    result = _related_impl(tree, A1_ID, None)
    returned = {t.id for t in result.topics}
    assert UNFILED_TOPIC_ID not in returned
    assert ROOT_TOPIC_ID not in returned
    assert T_ID not in returned                             # tombstoned
    assert T2_ID not in returned


def test_related_on_reserved_topic_is_invalid_argument(tree):
    for reserved_id in (ROOT_TOPIC_ID, UNFILED_TOPIC_ID):
        result = _related_impl(tree, reserved_id, None)
        assert isinstance(result, ErrorResponse)
        assert result.error.code == "invalid_argument"


def test_related_unknown_topic_uuid_is_not_found(tree):
    result = _related_impl(tree, "99999999-0000-4000-8000-000000000000", None)
    assert isinstance(result, ErrorResponse)
    assert result.error.code == "not_found"


def test_related_polymorphic_dispatch(tree):
    chunk_result = _related_impl(tree, "c_ap_1", None)      # not a UUID -> chunk path
    assert chunk_result.chunks is not None

    topic_result = _related_impl(tree, A_ID, None)          # UUID -> topic path
    assert topic_result.topics is not None


def test_related_tombstoned_topic_target_returns_empty_not_error(tree):
    # T2 has a NULL centroid -> nothing to compare, empty result (not an error)
    result = _related_impl(tree, T2_ID, None)
    assert isinstance(result, RelatedResult)
    assert result.topics == []


def test_superseded_by_falls_back_to_event_log(tree):
    result = _get_hierarchy_impl(tree, T2_ID, None)
    node = result.topics[0]
    assert node.tombstoned is True
    assert node.superseded_by == B_ID                       # from the topic_events row, column was NULL
