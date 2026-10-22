"""Cross-process determinism regression test (issue #54, extended).

The existing determinism tests in test_clustering.py all repeat calls
*within one already-running pytest process* - confirmed by reading them
directly. That never exercises the thing the organize-checkpointing
design (docs/organize-checkpointing-DESIGN.md §1d/§5.2) actually depends
on: does clustering reproduce identically in a brand-new process, the
exact situation a crash-and-resume implies. A manual investigation
found no divergence across 30 real subprocess runs (with and without
PYTHONHASHSEED pinned) - this test is that investigation's methodology,
kept permanently so a future regression (a library upgrade, a change to
_reduce_umap/_hdbscan_labels, a new source of process-local state) gets
caught automatically instead of silently reintroducing an unverified gap.

Marked `slow_cross_process` (see pytest.ini) - each iteration spawns a
real `python` subprocess that imports umap/hdbscan fresh, paying their
cold numba JIT-compile cost (~30s, observed directly during the original
investigation) with no way to share that cost across iterations, unlike
every other test in this suite. Skipped by default; run explicitly
(`pytest tests/test_clustering_cross_process.py -v -m slow_cross_process
--run-slow`) or in CI/before a release - see the `--run-slow` flag in
tests/conftest.py.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow_cross_process

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKER = Path(__file__).resolve().parent / "_cross_process_determinism_worker.py"

# 3, not the investigation's 10 - a real regression (if one were ever
# introduced) would show up on the very first divergent pair; 10 was for
# building confidence in a one-off investigation, not for bounding CI
# time on every release. Still genuinely 3 separate subprocesses, not a
# repeated in-process call.
N_RUNS = 3


@pytest.fixture(autouse=True)
def _require_run_slow_flag(request):
    if not request.config.getoption("--run-slow"):
        pytest.skip("slow cross-process test skipped - pass --run-slow to actually spawn subprocesses")


def _run_worker() -> dict:
    result = subprocess.run(
        [sys.executable, str(WORKER)],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, (
        f"worker subprocess failed (exit {result.returncode}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return json.loads(result.stdout)


def test_clustering_is_deterministic_across_real_process_boundaries():
    """Run the same fixed input through _reduce_umap + _hdbscan_labels in
    N_RUNS genuinely separate python processes; every one must produce
    byte-identical UMAP coordinates and the same label-invariant cluster
    partition, for both the spectral-init and forced-random-init paths."""
    runs = [_run_worker() for _ in range(N_RUNS)]

    for scenario in ("spectral", "random_init"):
        reduced_hashes = {r[scenario]["reduced_hash"] for r in runs}
        partitions = {tuple(tuple(g) for g in r[scenario]["partition"]) for r in runs}

        assert len(reduced_hashes) == 1, (
            f"scenario={scenario!r}: UMAP's reduced coordinates differed across "
            f"{N_RUNS} separate process invocations - cross-process determinism "
            f"has regressed. Hashes seen: {reduced_hashes}"
        )
        assert len(partitions) == 1, (
            f"scenario={scenario!r}: HDBSCAN's cluster partition differed across "
            f"{N_RUNS} separate process invocations, even though UMAP's own "
            f"coordinates matched - cross-process determinism has regressed "
            f"downstream of UMAP. Partitions seen: {partitions}"
        )
