"""Sandboxed, offline re-run of the clustering step, for the debug UI (J-13).

The point of this module is one guarantee: **no network call, no database
write, ever** - so a person can drag a slider (UMAP neighbours, HDBSCAN
min cluster size, the placement threshold...) and re-run clustering as
many times as they like without spending an LLM/embedding API call or
touching the live hierarchy.

That guarantee shapes the two things this module owns:

  - Config overrides are applied by monkeypatching the `organize.config`
    module's attributes for the duration of one call, under a process-
    wide lock (`clustering.py` reads `config.X` directly at call time -
    there is no parameter-threading path through it, so this is the only
    way to change its behaviour from outside without editing the module
    itself). The lock exists because two concurrent playground runs
    would otherwise stomp each other's overrides; a debug tool serialising
    its own requests is an acceptable price for not touching clustering.py's
    read sites.
  - Real chunk embeddings and text are read from the database (read-only:
    `clustering.load_chunk_inputs`), but labelling and the roll-up's
    title-embedding step are swapped for deterministic, offline stand-ins
    (`OfflineLLM`, `OfflineEmbedder` below) - extractive, hash-based, and
    clearly marked as such in every label they produce, so a trace can
    never be mistaken for a real run's output.
"""

import re
import threading
import uuid
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from jarvis_server.internal.embed.embed import Embedder
from jarvis_server.internal.llm.llm import LLMClient
from jarvis_server.organize import clustering, config

from jarvis_server.organize import trace

# Serialises playground runs against the config monkeypatch (see module
# docstring) - not a performance concern, just correctness under
# concurrent debug-UI requests. Shared by both the one-shot `run()` below
# and every step function in the "Sessions" section - a step call only
# ever holds it for the duration of its own UMAP/HDBSCAN/labelling work,
# never across a round-trip to the browser, so a stepped session and a
# one-shot run can safely interleave.
_run_lock = threading.Lock()

_STOPWORDS = frozenset("""
the a an and or but of to in on at for with from by is are was were be
been being this that these those it its as into over under out up down
not no yes if then than so such can could will would should may might
do does did has have had you your we our they them he she his her i
""".split())


def _offline_label(sample_texts: list[str]) -> tuple[str, str]:
    """A deterministic, offline stand-in for the LLM titling call:
    extractive top-3-word title from the sample text plus a description
    that says plainly this did not come from an LLM. Good enough to tell
    clusters apart in the playground UI; never written anywhere real."""
    words = re.findall(r"[a-zA-Z]{4,}", " ".join(sample_texts).lower())
    counts = Counter(w for w in words if w not in _STOPWORDS)
    top = [w.capitalize() for w, _ in counts.most_common(3)]
    title = " ".join(top) if top else "Unlabeled cluster"
    return title, f"[offline] extractive title from {len(sample_texts)} sample passage(s) - no LLM call made."


class OfflineLLM(LLMClient):
    """Never calls out. `complete()` is only reachable via `complete_json`
    (clustering.py's only entry point into the LLM), so this returns
    valid JSON directly rather than routing through it."""

    def complete(self, system: str, user: str, *, temperature: float = 0.2, max_tokens: int = 400) -> str:
        raise NotImplementedError("OfflineLLM must not be reached - clustering.py should call label_cluster/_label_parent overrides")


class OfflineEmbedder(Embedder):
    """Deterministic hash-based vectors for the roll-up's title-embedding
    step - same technique as tests/test_clustering.py's StubEmbedder.
    Never calls an API; identical text always yields identical vectors."""

    def __init__(self, dim: int = 1024):
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            rng = np.random.RandomState(abs(hash(t)) % (2**32))
            out.append(rng.rand(self.dim).tolist())
        return out


def _apply_overrides(overrides: dict) -> dict:
    """Monkeypatch config.<name> for every key in `overrides`. Returns the
    prior values so the caller can restore them. Raises ValueError on any
    key not in config.TUNABLE_NAMES - the playground must not be able to
    change anything outside the documented knob list (e.g. the reserved
    topic ids)."""
    unknown = set(overrides) - set(config.TUNABLE_NAMES)
    if unknown:
        raise ValueError(f"not a tunable parameter: {sorted(unknown)}")
    prior = {name: getattr(config, name) for name in overrides}
    for name, value in overrides.items():
        setattr(config, name, value)
    return prior


def _restore(prior: dict) -> None:
    for name, value in prior.items():
        setattr(config, name, value)


def run(
    sqlite_conn,
    *,
    overrides: Optional[dict] = None,
    restrict_to_chunk_ids: Optional[set[str]] = None,
) -> dict:
    """Re-cluster the corpus already sitting in `sqlite_conn` with
    `overrides` applied, entirely offline, and return the trace as a
    plain dict (JSON-ready for the debug UI).

    `sqlite_conn` is only ever read (`clustering.load_chunk_inputs`) -
    this function performs no INSERT/UPDATE/DELETE of any kind, so it is
    safe to point at the live database as well as a throwaway copy.
    """
    overrides = overrides or {}

    with _run_lock:
        prior = _apply_overrides(overrides)
        try:
            chunk_ids, matrix, texts = clustering.load_chunk_inputs(sqlite_conn, restrict_to_chunk_ids)
            with trace.recording("playground") as t:
                _label_cluster_patch = _make_offline_label_cluster()
                _label_parent_patch = _make_offline_label_parent()
                orig_label_cluster, orig_label_parent = clustering.label_cluster, clustering._label_parent
                clustering.label_cluster = _label_cluster_patch
                clustering._label_parent = _label_parent_patch
                try:
                    result = clustering.organize_vectors(
                        chunk_ids, matrix, texts,
                        OfflineEmbedder(), OfflineLLM(),
                        unfiled_topic_id="playground-unfiled",
                        requested_params=dict(overrides),
                    )
                finally:
                    clustering.label_cluster = orig_label_cluster
                    clustering._label_parent = orig_label_parent
            payload = t.to_dict()
            payload["summary"] = {
                "total_chunks": result.total_chunks,
                "topics": len(result.nodes),
                "roots": len(result.root_keys),
                "unfiled": len(result.unfiled.member_chunk_ids),
                "unfiled_pct": round(result.unfiled_pct, 1),
                "used_kmeans_fallback": result.used_kmeans_fallback,
                "low_confidence": result.low_confidence,
                "low_confidence_reason": result.low_confidence_reason,
            }
            return payload
        finally:
            _restore(prior)


def _make_offline_label_cluster():
    def _offline_label_cluster(sample_texts, llm_client, *, prior_label=None):
        return _offline_label(sample_texts)
    return _offline_label_cluster


def _make_offline_label_parent():
    def _offline_label_parent(child_labels, llm_client, sibling_titles):
        title, desc = _offline_label(child_labels)
        return f"{title} (parent)", desc
    return _offline_label_parent


# ---------------------------------------------------------------------
# Sessions: the base pass (UMAP, then HDBSCAN, then labelling) as three
# independently re-runnable stages.
# ---------------------------------------------------------------------
#
# `run()` above does the whole pipeline in one call - good for "does this
# corpus cluster reasonably", bad for building intuition about *why*: by
# the time it returns, UMAP and HDBSCAN have both already run and you can
# only look at the outcome. A Session instead holds the corpus's vectors
# in memory and exposes one function per stage, each reading the previous
# stage's cached output and writing its own - so a caller (the debug UI)
# can run just UMAP, look at the layout, then decide what to hand
# HDBSCAN, without recomputing UMAP every time an HDBSCAN parameter
# changes.
#
# Every stage function takes a `scope` string; today only "base" (the
# corpus's chunk embeddings) exists - the parameter is kept because a
# stage's own logic doesn't care what it's clustering, only
# `_resolve_scope_items` decides what "base" means, so a second scope
# could be reintroduced later without reshaping the three stage
# functions themselves.
#
# One global registry, capped, FIFO-evicted - this is a single-operator
# local debug tool, not a multi-tenant service; a handful of recent
# sessions is enough to compare a couple of parameter choices without
# holding every session's ~150MB chunk-vector copy in memory forever.
_MAX_SESSIONS = 5
_sessions: "OrderedDict[str, Session]" = OrderedDict()
_sessions_lock = threading.Lock()


@dataclass
class ScopeState:
    """One stage pipeline's cached progress for one scope. `items` is
    always None today (the only scope, "base", clusters the corpus's
    chunk embeddings directly, held on the Session instead) - kept as a
    field so a future non-chunk scope would only need to populate it."""
    items: Optional[list] = None
    ids: list = field(default_factory=list)          # chunk ids, in row order
    reduced: Optional[np.ndarray] = None
    umap_params: Optional[dict] = None
    labels: Optional[np.ndarray] = None
    outlier_scores: Optional[np.ndarray] = None
    hdbscan_params: Optional[dict] = None
    used_kmeans_fallback: bool = False
    result_node_keys: list = field(default_factory=list)


class Session:
    def __init__(self, chunk_ids: list[str], matrix: np.ndarray, texts: dict[str, str]):
        self.id = uuid.uuid4().hex[:12]
        self.chunk_ids = chunk_ids
        self.matrix = matrix
        self.texts = texts
        self.vec_by_id = {cid: matrix[i] for i, cid in enumerate(chunk_ids)}
        self.nodes_by_key: dict[str, "clustering.ClusterNode"] = {}
        self.top_level_keys: list[str] = []
        self.scopes: dict[str, ScopeState] = {}
        self.lock = threading.Lock()


def start_session(
    sqlite_conn,
    *,
    restrict_to_chunk_ids: Optional[set[str]] = None,
) -> dict:
    """Load the corpus's vectors into a new in-memory session and return
    its id. Read-only against `sqlite_conn`; the connection is not kept -
    every later step works off the in-memory copy."""
    with _run_lock:
        chunk_ids, matrix, texts = clustering.load_chunk_inputs(sqlite_conn, restrict_to_chunk_ids)
    session = Session(chunk_ids, matrix, texts)
    with _sessions_lock:
        _sessions[session.id] = session
        while len(_sessions) > _MAX_SESSIONS:
            _sessions.popitem(last=False)
    return {"session_id": session.id, "total_chunks": len(chunk_ids)}


def end_session(session_id: str) -> None:
    with _sessions_lock:
        _sessions.pop(session_id, None)


def _get_session(session_id: str) -> Session:
    with _sessions_lock:
        session = _sessions.get(session_id)
    if session is None:
        raise ValueError(
            "no active playground session with that id - it may have expired "
            "(only the last few sessions are kept); start a new one"
        )
    return session


def _resolve_scope_items(session: Session, scope: str) -> Optional[list]:
    """Only the "base" scope exists (operate on chunk embeddings
    directly) - always returns None. The Session model is written
    generically (see the module docstring) so a further scope could be
    added later, but only "base" is wired up today."""
    if scope == "base":
        return None
    raise ValueError(f"unknown scope: {scope!r}")


def _scope_ids_and_texts(scope_items: Optional[list], session: Session) -> tuple[list[str], dict[str, str]]:
    """(ids, preview text by id) for the active scope - chunk ids + their
    text for "base", the only scope currently wired up."""
    if scope_items is None:
        return session.chunk_ids, session.texts
    return (
        [n.key for n in scope_items],
        {n.key: f"{n.label}. {n.description}".strip() for n in scope_items},
    )


def run_umap_step(session_id: str, scope: str, *, overrides: Optional[dict] = None) -> dict:
    """Stage 1: reduce this scope's vectors with UMAP. Caches `reduced`
    on the scope and clears any HDBSCAN/labelling result already cached
    for it (changing this stage invalidates what came after)."""
    session = _get_session(session_id)
    with session.lock:
        items = _resolve_scope_items(session, scope)
        if items is None:
            vectors = session.matrix
        else:
            vectors = clustering._cluster_title_embeddings(items, OfflineEmbedder())
        ids, texts = _scope_ids_and_texts(items, session)

        with _run_lock:
            prior = _apply_overrides(overrides or {})
            try:
                params = clustering._scaled_clustering_params(vectors.shape[0])
                reduced = clustering._reduce_umap(vectors, params)
            finally:
                _restore(prior)

        state = ScopeState(items=items, ids=ids, reduced=reduced, umap_params=params)
        session.scopes[scope] = state

        coords, projection = trace.project_2d(reduced)
        points = [
            {"id": ids[i], "x": round(coords[i][0], 4), "y": round(coords[i][1], 4),
             "preview": (texts.get(ids[i], "") or "")[:trace.PREVIEW_CHARS]}
            for i in range(len(ids))
        ]
        return {
            "scope": scope, "stage": "umap", "projection": projection,
            "n_input_dims": int(reduced.shape[1]), "effective_params": params,
            "n_items": len(ids), "points": points,
        }


def run_hdbscan_step(session_id: str, scope: str, *, overrides: Optional[dict] = None) -> dict:
    """Stage 2: cluster this scope's cached UMAP output. Requires stage 1
    to have already run for this scope."""
    session = _get_session(session_id)
    with session.lock:
        state = session.scopes.get(scope)
        if state is None or state.reduced is None:
            raise ValueError(f"run the UMAP step for scope {scope!r} first")

        with _run_lock:
            prior = _apply_overrides(overrides or {})
            try:
                params = clustering._scaled_clustering_params(state.reduced.shape[0])
                labels, scores = clustering._hdbscan_labels(state.reduced, params)
                n_real = len({int(x) for x in labels if x != -1})
                used_kmeans = n_real < config.KMEANS_FALLBACK_MIN_CLUSTERS
                if used_kmeans:
                    labels, scores = clustering._kmeans_labels(state.reduced), np.zeros(state.reduced.shape[0])
            finally:
                _restore(prior)

        state.labels, state.outlier_scores = labels, scores
        state.hdbscan_params, state.used_kmeans_fallback = params, used_kmeans
        state.result_node_keys = []   # invalidate any prior labelling for this scope

        _, texts = _scope_ids_and_texts(state.items, session)
        coords, projection = trace.project_2d(state.reduced)
        points = [
            {"id": state.ids[i], "x": round(coords[i][0], 4), "y": round(coords[i][1], 4),
             "label": int(labels[i]), "outlier": round(float(scores[i]), 4),
             "preview": (texts.get(state.ids[i], "") or "")[:trace.PREVIEW_CHARS]}
            for i in range(len(state.ids))
        ]
        return {
            "scope": scope, "stage": "hdbscan", "projection": projection,
            "effective_params": params, "used_kmeans_fallback": used_kmeans,
            "n_clusters": len({int(x) for x in labels if int(x) != -1}),
            "n_noise": int(sum(1 for x in labels if int(x) == -1)),
            "points": points,
        }


def run_labeling_step(session_id: str, scope: str, *, overrides: Optional[dict] = None) -> dict:
    """Stage 3: title every group HDBSCAN found. For "base" this creates
    the base ClusterNodes (by HDBSCAN membership, pre-placement - matching
    how `organize_vectors` builds them)."""
    session = _get_session(session_id)
    with session.lock:
        state = session.scopes.get(scope)
        if state is None or state.labels is None:
            raise ValueError(f"run the HDBSCAN step for scope {scope!r} first")

        with _run_lock:
            prior = _apply_overrides(overrides or {})   # LABEL_SAMPLE_SIZE / LABEL_SAMPLE_CHARS
            try:
                result = _label_base_scope(session, state)
            finally:
                _restore(prior)
        return result


def _label_base_scope(session: Session, state: ScopeState) -> dict:
    labels = state.labels
    cards = []
    new_keys = []
    for label in sorted({int(x) for x in labels if int(x) != -1}):
        members = [session.chunk_ids[i] for i, x in enumerate(labels) if int(x) == label]
        member_vecs = np.asarray([session.vec_by_id[m] for m in members])
        centroid = member_vecs.mean(axis=0)
        sample_ids = clustering._representative_sample(members, member_vecs, centroid)
        sample_texts = [session.texts.get(m, "") for m in sample_ids]
        title, desc = _offline_label(sample_texts)
        key = f"base-{label}"
        node = clustering.ClusterNode(
            key=key, level=-1, label=title, description=desc,
            centroid=centroid.tolist(), member_chunk_ids=list(members), direct_chunk_ids=list(members),
        )
        session.nodes_by_key[key] = node
        new_keys.append(key)
        cards.append({
            "key": key, "cluster": label, "size": len(members), "title": title, "description": desc,
            "sample": [{"id": cid, "preview": t[:trace.PREVIEW_CHARS]} for cid, t in zip(sample_ids, sample_texts)],
        })
    session.top_level_keys = list(new_keys)
    state.result_node_keys = new_keys
    return {"scope": "base", "stage": "label", "kind": "base", "clusters": cards, "top_level_keys": new_keys}
