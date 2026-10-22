"""J-09 matching + structural-event derivation (pure, no I/O).

Hand-built before/after cluster sets with obvious cardinality. Every
event must be *derived* from the new<->old mapping shape (spec §6), so
each test fixes the mapping and asserts the exact event stream.
"""

import pytest

from jarvis_server.organize import config
from jarvis_server.organize.matching import (
    NewCluster,
    PriorTopic,
    match_and_derive_events,
)


@pytest.fixture
def uuids():
    counter = {"n": 0}

    def mint():
        counter["n"] += 1
        return f"born-{counter['n']}"

    return mint


def _new(key, centroid, members, label="L", parent_key=None):
    return NewCluster(key, list(centroid), set(members), label, f"{label} desc", parent_key)


def _prior(tid, centroid, members, name="L", parent=None, version=1):
    return PriorTopic(tid, list(centroid), set(members), name, f"{name} desc", parent, version)


# ---------------------------------------------------------------------
# 1:1
# ---------------------------------------------------------------------

def test_one_to_one_stable_carries_uuid_and_emits_nothing(uuids):
    new = [_new("a", [1, 0, 0], {"c1", "c2", "c3"})]
    prior = [_prior("uuid-a", [1, 0, 0], {"c1", "c2", "c3"})]

    r = match_and_derive_events(new, prior, new_uuid_fn=uuids)

    assert r.assignment == {"a": "uuid-a"}
    assert r.carried_from == {"a": "uuid-a"}
    assert r.events == []
    assert r.materially_changed_keys == set()


def test_one_to_one_membership_drift_flags_materially_changed(uuids):
    new = [_new("a", [1, 0, 0], {"c1", "c9", "c8", "c7"})]        # only c1 shared
    prior = [_prior("uuid-a", [1, 0, 0], {"c1", "c2", "c3", "c4"})]

    r = match_and_derive_events(new, prior, new_uuid_fn=uuids)

    assert r.assignment == {"a": "uuid-a"}                        # still the same node
    assert "a" in r.materially_changed_keys                       # ...but must be re-labelled


def test_one_to_one_reparent_emits_reparent_event(uuids):
    # child stays 1:1, but its parent changes from uuid-p1 to the new p2
    new = [
        _new("p2", [0, 1, 0], {"c9"}, label="P2"),
        _new("child", [1, 0, 0], {"c1", "c2"}, label="Child", parent_key="p2"),
    ]
    prior = [
        _prior("uuid-p1", [0, 0, 1], {"c8"}, name="P1"),
        _prior("uuid-child", [1, 0, 0], {"c1", "c2"}, name="Child", parent="uuid-p1"),
    ]

    r = match_and_derive_events(new, prior, new_uuid_fn=uuids)

    reparents = [e for e in r.events if e.event_type == "reparent"]
    assert len(reparents) == 1
    assert reparents[0].topic_id == "uuid-child"
    assert reparents[0].detail["from"] == "uuid-p1"
    assert reparents[0].detail["to"] == r.assignment["p2"]


# ---------------------------------------------------------------------
# born / tombstone
# ---------------------------------------------------------------------

def test_first_sync_everything_is_born(uuids):
    new = [_new("a", [1, 0, 0], {"c1"}), _new("b", [0, 1, 0], {"c2"})]

    r = match_and_derive_events(new, [], new_uuid_fn=uuids)

    assert r.summary() == {"born": 2}
    assert set(r.born_keys) == {"a", "b"}
    assert r.carried_from == {}


def test_prior_with_no_match_is_tombstoned_not_deleted(uuids):
    new = [_new("a", [1, 0, 0], {"c1", "c2"})]
    prior = [
        _prior("uuid-a", [1, 0, 0], {"c1", "c2"}),
        _prior("uuid-gone", [0, 1, 0], {"c8", "c9"}, name="Obsolete"),
    ]

    r = match_and_derive_events(new, prior, new_uuid_fn=uuids)

    assert r.tombstoned_topic_ids == ["uuid-gone"]
    assert [e.event_type for e in r.events] == ["tombstone"]
    assert "uuid-a" not in r.tombstoned_topic_ids


def test_new_with_no_match_is_born_alongside_a_stable_node(uuids):
    new = [
        _new("a", [1, 0, 0], {"c1", "c2"}),
        _new("fresh", [0, 1, 0], {"c8", "c9"}, label="Fresh"),
    ]
    prior = [_prior("uuid-a", [1, 0, 0], {"c1", "c2"})]

    r = match_and_derive_events(new, prior, new_uuid_fn=uuids)

    assert r.summary() == {"born": 1}
    assert r.born_keys == {"fresh"}
    assert r.assignment["a"] == "uuid-a"


# ---------------------------------------------------------------------
# split / merge
# ---------------------------------------------------------------------

def test_one_prior_two_new_is_a_split(uuids):
    prior = [_prior("uuid-animals", [1, 0, 0], {"c1", "c2", "c3", "c4"}, name="Animals")]
    new = [
        _new("cats", [1, 0, 0], {"c1", "c2", "c3"}, label="Cats"),     # bigger overlap -> survivor
        _new("dogs", [1, 0.1, 0], {"c4"}, label="Dogs"),
    ]

    r = match_and_derive_events(new, prior, new_uuid_fn=uuids)

    assert r.summary() == {"split": 1, "born": 1}
    assert r.assignment["cats"] == "uuid-animals"                  # survivor keeps the UUID
    assert r.assignment["dogs"].startswith("born-")
    split = next(e for e in r.events if e.event_type == "split")
    assert split.topic_id == "uuid-animals"
    assert split.detail["split_into"] == [r.assignment["dogs"]]
    born = next(e for e in r.events if e.event_type == "born")
    assert born.detail["split_from"] == "uuid-animals"
    assert {"cats", "dogs"} <= r.materially_changed_keys


def test_two_prior_one_new_is_a_merge(uuids):
    prior = [
        _prior("uuid-cats", [1, 0, 0], {"c1", "c2", "c3"}, name="Cats"),   # bigger overlap -> survivor
        _prior("uuid-dogs", [1, 0.1, 0], {"c4"}, name="Dogs"),
    ]
    new = [_new("animals", [1, 0, 0], {"c1", "c2", "c3", "c4"}, label="Animals")]

    r = match_and_derive_events(new, prior, new_uuid_fn=uuids)

    assert r.summary() == {"merge": 1, "tombstone": 1}
    assert r.assignment["animals"] == "uuid-cats"
    assert r.tombstoned_topic_ids == ["uuid-dogs"]
    tomb = next(e for e in r.events if e.event_type == "tombstone")
    assert tomb.detail["merged_into"] == "uuid-cats"
    merge = next(e for e in r.events if e.event_type == "merge")
    assert merge.detail["merged_from"] == ["uuid-dogs"]


# ---------------------------------------------------------------------
# threshold gate + determinism + N:M
# ---------------------------------------------------------------------

def test_pair_below_tau_does_not_match(uuids):
    # orthogonal centroids, disjoint members -> score 0, well below tau
    new = [_new("a", [1, 0, 0], {"c1", "c2"})]
    prior = [_prior("uuid-x", [0, 1, 0], {"c8", "c9"}, name="X")]

    r = match_and_derive_events(new, prior, new_uuid_fn=uuids)

    assert r.summary() == {"born": 1, "tombstone": 1}
    assert r.assignment["a"].startswith("born-")


def test_event_stream_is_deterministic(uuids):
    prior = [
        _prior("uuid-a", [1, 0, 0], {"c1", "c2", "c3", "c4"}, name="A"),
        _prior("uuid-b", [0, 1, 0], {"c5", "c6"}, name="B"),
    ]

    def build_new():
        return [
            _new("a1", [1, 0, 0], {"c1", "c2"}, label="A1"),
            _new("a2", [1, 0.1, 0], {"c3", "c4"}, label="A2"),
            _new("c", [0, 0, 1], {"c9"}, label="C"),
        ]

    r1 = match_and_derive_events(build_new(), prior, new_uuid_fn=lambda: "X")
    r2 = match_and_derive_events(build_new(), prior, new_uuid_fn=lambda: "X")

    assert [(e.event_type, e.topic_id, e.detail) for e in r1.events] == \
           [(e.event_type, e.topic_id, e.detail) for e in r2.events]
    assert r1.assignment == r2.assignment


def test_many_to_many_component_decomposes_to_merge_plus_split(uuids, caplog):
    # 2 priors and 2 new all mutually overlapping -> one messy component
    prior = [
        _prior("uuid-p1", [1, 0, 0], {"c1", "c2", "c3"}, name="P1"),
        _prior("uuid-p2", [1, 0.05, 0], {"c2", "c3", "c4"}, name="P2"),
    ]
    new = [
        _new("n1", [1, 0, 0], {"c1", "c2", "c3", "c4"}, label="N1"),
        _new("n2", [1, 0.05, 0], {"c2", "c3"}, label="N2"),
    ]

    r = match_and_derive_events(new, prior, new_uuid_fn=uuids)

    kinds = r.summary()
    assert kinds.get("merge") == 1 and kinds.get("split") == 1
    assert kinds.get("tombstone") == 1        # the non-survivor prior
    assert any("prior : " in rec.message or "decomposing" in rec.message for rec in caplog.records)


def test_relabel_thresholds_are_config_driven():
    assert 0.0 < config.RECONCILE_MATCH_TAU < 1.0
    assert 0.0 < config.RECONCILE_CENTROID_WEIGHT < 1.0
    assert 0.0 < config.RELABEL_JACCARD_DROP <= 1.0
