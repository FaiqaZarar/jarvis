"""Standalone worker for tests/test_clustering_cross_process.py.

Not a test module itself (deliberately not named test_*.py, so pytest
never collects it) - a fresh `python` subprocess runs this file exactly
as it would `python -m jarvis.cmd.daemon`, so any answer it produces
reflects a real, separate process/interpreter, not a repeated in-process
call. Prints one JSON line to stdout: the two scenarios' clustering
output, keyed the same way for every invocation, so two separate runs'
stdout can be compared directly.

Same two scenarios as the manual cross-process investigation that found
issue #54's fix holds across processes (docs/organize-checkpointing-DESIGN.md
§1d/§5.2): one exercising UMAP's default "spectral" init
(n_neighbors=14), one exercising the small-graph "random" init issue #54
itself forces (n_neighbors=2) - both mirror an existing single-process
determinism test's exact shape (`_blobs` args), so this is a direct,
comparable extension of that coverage across a process boundary, not a
new scenario invented for this file.
"""

import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from jarvis_server.organize import clustering  # noqa: E402
from tests.test_clustering import _blobs  # noqa: E402

SCENARIOS = {
    # mirrors test_organize_membership_is_deterministic's shape
    "spectral": dict(n_clusters=3, per_cluster=9, n_outliers=2, seed=7),
    # mirrors test_small_corpus_base_clustering_is_deterministic's shape
    # (issue #54's exact scenario: n_neighbors=2 forces "random" init)
    "random_init": dict(n_clusters=2, per_cluster=2, n_outliers=1, seed=11),
}


def _partition(labels) -> list:
    """Label-invariant grouping of row indices - which points ended up
    together, regardless of what integer HDBSCAN happened to number the
    group as. This is the signal that actually matters (matches the
    organize-checkpointing design's content-fingerprint approach)."""
    groups: dict[int, list[int]] = {}
    for i, lab in enumerate(labels):
        groups.setdefault(int(lab), []).append(i)
    return sorted(tuple(sorted(v)) for v in groups.values())


def run_one(params: dict) -> dict:
    matrix, _truth = _blobs(**params)
    reduce_params = clustering._scaled_clustering_params(matrix.shape[0])
    reduced = clustering._reduce_umap(matrix, reduce_params)
    labels, _scores = clustering._hdbscan_labels(reduced, reduce_params)
    return {
        "n_neighbors": reduce_params["n_neighbors"],
        "reduced_hash": hashlib.sha256(reduced.tobytes()).hexdigest(),
        "partition": _partition(labels),
    }


def main() -> None:
    print(json.dumps({name: run_one(p) for name, p in SCENARIOS.items()}))


if __name__ == "__main__":
    main()
