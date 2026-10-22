"""Step-by-step trace of one `organize()` run, for the debug UI (J-13).

`clustering.organize()` returns only its *result* - the finished tree. The
intermediate state that explains how that tree was arrived at (the UMAP
coordinates HDBSCAN actually saw, which points it called noise, what
excerpts the labelling LLM was shown, which groups the roll-up formed,
the similarity at every step of each chunk's placement descent) is all
local to the call and lost on return. This module lets a caller opt into
capturing it.

Tracing is OFF unless a caller opens `recording()`. Every `record_*`
below is a module-level function that returns immediately when no
recorder is active, so the production path pays one ContextVar lookup and
nothing else - and, importantly, the hooks in clustering.py stay pure
observation: nothing here feeds back into a clustering decision.

A ContextVar (not a plain global) so a trace stays bound to the task /
thread that opened it - the debug UI is an async server that runs a
playground clustering in a worker thread while its other routes keep
serving.
"""

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np

from jarvis_server.internal.logging.logging_config import get_logger

logger = get_logger(__name__)

TRACE_VERSION = 1

# Chunk text kept per point for the scatter tooltip. Long enough to
# recognise the passage, short enough that a few thousand points stay a
# reasonably sized JSON payload for the browser.
PREVIEW_CHARS = 240

# Placement produces one descent path per chunk. All of them are recorded
# up to this many; past it the trace keeps the count but drops the paths,
# so a large corpus cannot turn the trace file into hundreds of MB.
MAX_PLACEMENT_PATHS = 3000


@dataclass
class OrganizeTrace:
    """Everything one organize() run did, in the order it did it."""

    source: str
    requested_params: dict = field(default_factory=dict)
    config_snapshot: dict = field(default_factory=dict)
    corpus: dict = field(default_factory=dict)
    umap: dict = field(default_factory=dict)
    base: dict = field(default_factory=dict)
    labeling: list[dict] = field(default_factory=list)
    rollup: dict = field(default_factory=lambda: {"rounds": [], "stopped_reason": None})
    placement: dict = field(default_factory=lambda: {"paths": [], "counts": {}, "truncated": False})
    tree: dict = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "version": TRACE_VERSION,
            "created_at": self.created_at,
            "source": self.source,
            "requested_params": self.requested_params,
            "config_snapshot": self.config_snapshot,
            "corpus": self.corpus,
            "umap": self.umap,
            "base": self.base,
            "labeling": self.labeling,
            "rollup": self.rollup,
            "placement": self.placement,
            "tree": self.tree,
        }


_active: ContextVar[Optional[OrganizeTrace]] = ContextVar("organize_trace", default=None)


def active() -> Optional[OrganizeTrace]:
    return _active.get()


@contextmanager
def recording(source: str):
    """Capture the organize() run inside this block. Yields the trace."""
    trace = OrganizeTrace(source=source)
    token = _active.set(trace)
    try:
        yield trace
    finally:
        _active.reset(token)


def write(trace: OrganizeTrace, path: Path) -> None:
    """Serialise a trace to `path` (atomically - the debug UI polls it)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(trace.to_dict()), encoding="utf-8")
    tmp.replace(path)
    logger.info("organize trace written to %s", path)


# ---------------------------------------------------------------------
# Recording hooks - each is a no-op when tracing is off
# ---------------------------------------------------------------------

def record_params(requested: dict, config_snapshot: dict) -> None:
    trace = _active.get()
    if trace is None:
        return
    trace.requested_params = dict(requested)
    trace.config_snapshot = dict(config_snapshot)


def record_corpus(**fields: Any) -> None:
    trace = _active.get()
    if trace is None:
        return
    trace.corpus.update(fields)


def project_2d(reduced: np.ndarray) -> tuple[list[list[float]], str]:
    """A 2-D view of the space the clusterer actually worked in.

    UMAP reduces to `n_components` (5 by default), so the clustering
    happens somewhere we cannot draw. Rather than run a *second*, separate
    2-D UMAP - which would draw a layout no clustering decision was ever
    made in - this takes the principal 2-D plane of the very same reduced
    matrix HDBSCAN was handed. It is a linear projection of the real
    thing: points that sit together on screen sat together in the space
    that produced the labels.
    """
    if reduced.shape[1] <= 2:
        pad = np.zeros((reduced.shape[0], 2))
        pad[:, : reduced.shape[1]] = reduced
        return pad.tolist(), "direct"
    centered = reduced - reduced.mean(axis=0)
    # full_matrices=False: we only need the top-2 right singular vectors.
    _, _, vt = np.linalg.svd(centered, full_matrices=False)
    return (centered @ vt[:2].T).tolist(), "pca2"


def record_umap(
    *,
    context: str,
    chunk_ids: list[str],
    reduced: np.ndarray,
    effective_params: dict,
    texts: Optional[dict[str, str]] = None,
) -> None:
    """Step 1 of base clustering: where UMAP placed every point, BEFORE
    HDBSCAN has looked at it or assigned any label. Same coordinates
    `record_base_clustering` (step 2) will draw with cluster colors - this
    is the "before" half of that before/after, so the UI can show UMAP's
    layout and HDBSCAN's clusters as two distinct, sequential steps rather
    than one merged scatter plot.
    """
    trace = _active.get()
    if trace is None:
        return

    coords, projection = project_2d(reduced)
    texts = texts or {}
    points = [
        {
            "id": chunk_id,
            "x": round(coords[i][0], 4),
            "y": round(coords[i][1], 4),
            "preview": (texts.get(chunk_id, "") or "")[:PREVIEW_CHARS],
        }
        for i, chunk_id in enumerate(chunk_ids)
    ]
    payload = {
        "context": context,
        "projection": projection,
        "n_input_dims": int(reduced.shape[1]),
        "effective_params": dict(effective_params),
        "points": points,
    }
    if context == "base":
        trace.umap = payload
    else:
        trace.rollup.setdefault("pending_umap_pass", payload)


def record_base_clustering(
    *,
    context: str,
    chunk_ids: list[str],
    reduced: np.ndarray,
    labels: np.ndarray,
    outlier_scores: np.ndarray,
    effective_params: dict,
    used_kmeans_fallback: bool,
    texts: Optional[dict[str, str]] = None,
) -> None:
    """Step 2 of base clustering: what HDBSCAN (or the k-means fallback)
    called each of those same points - the "after" half of the
    UMAP/HDBSCAN split (see `record_umap`).

    Recorded for the base pass over chunks *and* for each roll-up pass
    over cluster titles (`context` says which); the roll-up passes are
    attached to their round by `record_rollup_round`.
    """
    trace = _active.get()
    if trace is None:
        return

    coords, projection = project_2d(reduced)
    texts = texts or {}
    points = [
        {
            "id": chunk_id,
            "x": round(coords[i][0], 4),
            "y": round(coords[i][1], 4),
            "label": int(labels[i]),
            "outlier": round(float(outlier_scores[i]), 4),
            "preview": (texts.get(chunk_id, "") or "")[:PREVIEW_CHARS],
        }
        for i, chunk_id in enumerate(chunk_ids)
    ]
    payload = {
        "context": context,
        "projection": projection,
        "n_input_dims": int(reduced.shape[1]),
        "effective_params": dict(effective_params),
        "used_kmeans_fallback": used_kmeans_fallback,
        "n_clusters": len({int(x) for x in labels if int(x) != -1}),
        "n_noise": int(sum(1 for x in labels if int(x) == -1)),
        "points": points,
    }
    if context == "base":
        trace.base = payload
    else:
        trace.rollup.setdefault("pending_pass", payload)


def record_cluster_label(
    *,
    cluster: int,
    size: int,
    sample: list[dict],
    title: str,
    description: str,
) -> None:
    """What the labelling LLM was shown for one base cluster, and what it
    gave back. `sample` is the representative excerpts, verbatim."""
    trace = _active.get()
    if trace is None:
        return
    trace.labeling.append({
        "cluster": cluster,
        "size": size,
        "sample": sample,
        "title": title,
        "description": description,
    })


def record_rollup_round(
    *,
    round_index: int,
    inputs: list[dict],
    groups: list[dict],
) -> None:
    trace = _active.get()
    if trace is None:
        return
    trace.rollup["rounds"].append({
        "round": round_index,
        "inputs": inputs,
        "groups": groups,
        "umap_pass": trace.rollup.pop("pending_umap_pass", None),
        "pass": trace.rollup.pop("pending_pass", None),
    })


def record_rollup_stopped(reason: str) -> None:
    trace = _active.get()
    if trace is None:
        return
    trace.rollup["stopped_reason"] = reason
    trace.rollup.pop("pending_umap_pass", None)
    trace.rollup.pop("pending_pass", None)


def record_placement(
    *,
    chunk_id: str,
    steps: list[dict],
    target: Optional[str],
    reason: str,
) -> None:
    """One chunk's root-to-leaf descent: the best-scoring node at each
    level and whether it cleared the threshold."""
    trace = _active.get()
    if trace is None:
        return
    counts = trace.placement["counts"]
    counts[reason] = counts.get(reason, 0) + 1
    if len(trace.placement["paths"]) >= MAX_PLACEMENT_PATHS:
        trace.placement["truncated"] = True
        return
    trace.placement["paths"].append({
        "id": chunk_id,
        "steps": steps,
        "target": target,
        "reason": reason,
    })


def record_tree(
    *,
    nodes: list[dict],
    roots: list[str],
    unfiled: dict,
    pruned: list[dict],
    threshold: float,
) -> None:
    trace = _active.get()
    if trace is None:
        return
    trace.tree = {
        "nodes": nodes,
        "roots": roots,
        "unfiled": unfiled,
        "pruned": pruned,
    }
    trace.placement["threshold"] = threshold
