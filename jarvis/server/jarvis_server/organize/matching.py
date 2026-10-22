"""Cluster<->Topic matching and structural-event derivation (J-09).

Pure. No database, no graph, no network - given the freshly re-clustered
nodes and the prior Topics, this returns the list of structural mutation
events and the UUID each surviving new node should carry. `reconcile.py`
does all the I/O around it.

Approach (see docs/J09_DESIGN.md §0, confirmed):

  * Score every (new, prior) pair by a blend of centroid-cosine and
    membership-Jaccard.
  * Keep the pairs at or above tau -> a bipartite match graph.
  * Read each connected component's cardinality and map it directly to an
    event, per spec §6:
        0 prior : 1 new    -> born
        1 prior : 0 new    -> tombstone
        1 prior : 1 new    -> same node (rename? reparent?)
        1 prior : N new    -> split
        N prior : 1 new    -> merge
        N prior : M new     -> decomposed to merge + split, logged loudly
    (Strict 1:1 Hungarian assignment cannot express split/merge from
    cardinality - that is the whole reason this is many-to-many.)

Nothing here knows about `Unfiled` or tombstoned Topics; `reconcile.py`
filters those out of `prior` before calling in.
"""

from dataclasses import dataclass, field
from typing import Optional

from jarvis_server.internal.logging.logging_config import get_logger
from jarvis_server.organize import config

logger = get_logger(__name__)

# Guard-rail: a connected component spanning more than this many nodes on
# either side almost always means tau is too low, not that a genuine
# N:M restructure happened. Processed anyway, but logged as an error.
_MANY_TO_MANY_SANITY_LIMIT = 6


# ---------------------------------------------------------------------
# Inputs / outputs
# ---------------------------------------------------------------------

@dataclass
class NewCluster:
    """A node from this sync's fresh clustering (adapted from a
    ClusterNode by reconcile.py). `key` is transient; `parent_key` refers
    to another NewCluster's key, or None for a root."""
    key: str
    centroid: list[float]
    member_chunk_ids: set[str]
    label: str
    description: str
    parent_key: Optional[str] = None


@dataclass
class PriorTopic:
    """An existing Topic as of the last successful sync (loaded from the
    SQLite cache by reconcile.py). Excludes `Unfiled`, the synthetic root,
    and already-tombstoned Topics.

    `version` is carried for reconcile.py's bookkeeping only - matching
    itself never reads it."""
    topic_id: str
    centroid: list[float]
    member_chunk_ids: set[str]
    name: str
    description: str
    parent_topic_id: Optional[str] = None
    version: int = 1


@dataclass
class MutationEvent:
    """One row destined for `topic_events`. `topic_id` is the UUID the
    event is *about* (the surviving/born/tombstoned node)."""
    event_type: str                      # born | tombstone | rename | reparent | split | merge
    topic_id: str
    detail: dict = field(default_factory=dict)


@dataclass
class MatchResult:
    events: list[MutationEvent]
    assignment: dict[str, str]           # new key -> UUID it should carry
    carried_from: dict[str, str]         # new key -> prior topic_id it continues (1:1 / split / merge survivor)
    tombstoned_topic_ids: list[str]
    materially_changed_keys: set[str]    # new keys that must be re-labelled (spec §6)
    born_keys: set[str]

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for e in self.events:
            counts[e.event_type] = counts.get(e.event_type, 0) + 1
        return counts


# ---------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------

def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _blended_score(new: NewCluster, prior: PriorTopic) -> float:
    w = config.RECONCILE_CENTROID_WEIGHT
    return w * _cosine(new.centroid, prior.centroid) + (1.0 - w) * _jaccard(
        new.member_chunk_ids, prior.member_chunk_ids
    )


# ---------------------------------------------------------------------
# Union-find over the bipartite match graph
# ---------------------------------------------------------------------

class _UnionFind:
    def __init__(self, items: list[str]):
        self._parent = {x: x for x in items}

    def find(self, x: str) -> str:
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb


# ---------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------

def match_and_derive_events(
    new_clusters: list[NewCluster],
    prior_topics: list[PriorTopic],
    *,
    new_uuid_fn,
) -> MatchResult:
    """Match `new_clusters` to `prior_topics` and derive the structural
    events. `new_uuid_fn()` mints a fresh UUID string for a born node
    (injected so tests can make it deterministic).

    Deterministic: new clusters are processed in `key` order, priors in
    `topic_id` order, and every tie-break is explicit - the event stream
    for identical inputs is byte-identical (protects `topic_events` from
    RNG-manufactured churn).
    """
    new_by_key = {n.key: n for n in sorted(new_clusters, key=lambda n: n.key)}
    prior_by_id = {p.topic_id: p for p in sorted(prior_topics, key=lambda p: p.topic_id)}

    # --- resolved parent UUID for each new node, once assignment is known ---
    assignment: dict[str, str] = {}
    carried_from: dict[str, str] = {}
    events: list[MutationEvent] = []
    tombstoned: list[str] = []
    materially_changed: set[str] = set()
    born: set[str] = set()

    # --- build edges at/above tau ---
    tau = config.RECONCILE_MATCH_TAU
    new_tag = {k: f"n:{k}" for k in new_by_key}
    prior_tag = {i: f"p:{i}" for i in prior_by_id}
    uf = _UnionFind(list(new_tag.values()) + list(prior_tag.values()))
    edges: list[tuple[str, str, float]] = []
    for nk, n in new_by_key.items():
        for pid, p in prior_by_id.items():
            score = _blended_score(n, p)
            if score >= tau:
                edges.append((nk, pid, score))
                uf.union(new_tag[nk], prior_tag[pid])

    logger.info(
        "reconcile.match: %d new, %d prior, %d candidate edges (tau=%.2f, w=%.2f)",
        len(new_by_key), len(prior_by_id), len(edges), tau, config.RECONCILE_CENTROID_WEIGHT,
    )

    # --- group into connected components ---
    components: dict[str, dict[str, set]] = {}
    for nk in new_by_key:
        components.setdefault(uf.find(new_tag[nk]), {"new": set(), "prior": set()})["new"].add(nk)
    for pid in prior_by_id:
        components.setdefault(uf.find(prior_tag[pid]), {"new": set(), "prior": set()})["prior"].add(pid)

    edge_score = {(nk, pid): s for nk, pid, s in edges}

    def overlap(nk: str, pid: str) -> int:
        return len(new_by_key[nk].member_chunk_ids & prior_by_id[pid].member_chunk_ids)

    # --- classify each component (deterministic order) ---
    for _, comp in sorted(components.items(), key=lambda kv: (
        sorted(kv[1]["new"]), sorted(kv[1]["prior"])
    )):
        comp_new = sorted(comp["new"])
        comp_prior = sorted(comp["prior"])

        if comp_new and not comp_prior:
            for nk in comp_new:                       # isolated new node(s)
                _record_born(nk, events, assignment, born, new_uuid_fn)
            continue

        if comp_prior and not comp_new:
            for pid in comp_prior:                    # isolated prior node(s)
                events.append(MutationEvent("tombstone", pid, {"reason": "no matching cluster this sync"}))
                tombstoned.append(pid)
            continue

        if len(comp_new) == 1 and len(comp_prior) == 1:
            _record_one_to_one(
                comp_new[0], comp_prior[0], new_by_key, prior_by_id,
                assignment, carried_from, materially_changed,
            )
            continue

        if len(comp_prior) == 1 and len(comp_new) > 1:
            _record_split(
                comp_prior[0], comp_new, prior_by_id, overlap,
                events, assignment, carried_from, materially_changed, born, new_uuid_fn,
            )
            continue

        if len(comp_new) == 1 and len(comp_prior) > 1:
            _record_merge(
                comp_new[0], comp_prior, prior_by_id, overlap,
                events, assignment, carried_from, materially_changed, tombstoned,
            )
            continue

        # N prior : M new - decompose to a merge into the best new node,
        # then a split out to the rest. Loud: this usually means tau is low.
        log = logger.error if max(len(comp_new), len(comp_prior)) > _MANY_TO_MANY_SANITY_LIMIT else logger.warning
        log(
            "reconcile.match: %d-prior : %d-new component - decomposing as merge+split "
            "(new=%s prior=%s). If this recurs, RECONCILE_MATCH_TAU is probably too low.",
            len(comp_prior), len(comp_new), comp_new, comp_prior,
        )
        best_nk = max(comp_new, key=lambda nk: (
            max((edge_score.get((nk, pid), 0.0) for pid in comp_prior), default=0.0), nk
        ))
        _record_merge(
            best_nk, comp_prior, prior_by_id, overlap,
            events, assignment, carried_from, materially_changed, tombstoned,
        )
        survivor_uuid = assignment[best_nk]
        other_new = [nk for nk in comp_new if nk != best_nk]
        for nk in other_new:
            _record_born(nk, events, assignment, born, new_uuid_fn,
                         detail={"split_from": survivor_uuid})
            materially_changed.add(nk)
        events.append(MutationEvent("split", survivor_uuid,
                                    {"split_into": sorted(assignment[nk] for nk in other_new)}))

    # --- reparent detection: needs the full assignment, so a second pass ---
    _emit_reparent_events(new_by_key, prior_by_id, assignment, carried_from, events, materially_changed)

    return MatchResult(
        events=events,
        assignment=assignment,
        carried_from=carried_from,
        tombstoned_topic_ids=sorted(set(tombstoned)),
        materially_changed_keys=materially_changed,
        born_keys=born,
    )


# ---------------------------------------------------------------------
# per-component recorders
# ---------------------------------------------------------------------

def _record_born(nk, events, assignment, born, new_uuid_fn, detail=None):
    uuid_str = new_uuid_fn()
    assignment[nk] = uuid_str
    born.add(nk)
    events.append(MutationEvent("born", uuid_str, detail or {}))


def _record_one_to_one(nk, pid, new_by_key, prior_by_id, assignment, carried_from, materially_changed):
    assignment[nk] = pid
    carried_from[nk] = pid
    n, p = new_by_key[nk], prior_by_id[pid]

    self_jaccard = _jaccard(n.member_chunk_ids, p.member_chunk_ids)
    drift = 1.0 - _cosine(n.centroid, p.centroid)
    if self_jaccard < config.RELABEL_JACCARD_DROP or drift > config.RELABEL_CENTROID_DRIFT:
        materially_changed.add(nk)
    # a rename event is emitted by reconcile.py only if the *final* name
    # (after the re-label pick) differs from prior - it owns that decision.


def _record_split(prior_id, comp_new, prior_by_id, overlap, events,
                  assignment, carried_from, materially_changed, born, new_uuid_fn):
    survivor = max(comp_new, key=lambda nk: (overlap(nk, prior_id), nk))
    assignment[survivor] = prior_id
    carried_from[survivor] = prior_id
    materially_changed.add(survivor)

    others = [nk for nk in comp_new if nk != survivor]
    for nk in others:
        _record_born(nk, events, assignment, born, new_uuid_fn,
                     detail={"split_from": prior_id})
        materially_changed.add(nk)
    events.append(MutationEvent("split", prior_id,
                                {"split_into": sorted(assignment[nk] for nk in others)}))


def _record_merge(new_key, comp_prior, prior_by_id, overlap, events,
                  assignment, carried_from, materially_changed, tombstoned):
    survivor = max(comp_prior, key=lambda pid: (overlap(new_key, pid), pid))
    assignment[new_key] = survivor
    carried_from[new_key] = survivor
    materially_changed.add(new_key)

    losers = [pid for pid in comp_prior if pid != survivor]
    for pid in losers:
        events.append(MutationEvent("tombstone", pid,
                                    {"merged_into": survivor}))
        tombstoned.append(pid)
    events.append(MutationEvent("merge", survivor, {"merged_from": sorted(losers)}))


def _emit_reparent_events(new_by_key, prior_by_id, assignment, carried_from, events, materially_changed):
    """A carried-forward node whose parent UUID now differs from its prior
    parent UUID gets a `reparent` event. Runs after assignment so both
    parent UUIDs are resolvable."""
    for nk, prior_id in sorted(carried_from.items()):
        n = new_by_key[nk]
        new_parent_uuid = assignment.get(n.parent_key) if n.parent_key else None
        prior_parent_uuid = prior_by_id[prior_id].parent_topic_id
        if new_parent_uuid != prior_parent_uuid:
            events.append(MutationEvent("reparent", assignment[nk], {
                "from": prior_parent_uuid, "to": new_parent_uuid,
            }))
            materially_changed.add(nk)
