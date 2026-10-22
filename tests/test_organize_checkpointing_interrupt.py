"""Real interrupt-and-resume test for organize checkpointing
(docs/organize-checkpointing-DESIGN.md).

Not a mocked exception, not a simulated failure: this spawns a real
`python` subprocess running `clustering.organize()` against a real
sqlite file, waits for genuine, externally-observable partial progress
(an LLM call actually starting), kills that process outright (the same
`os.kill(pid, signal.SIGTERM)` mechanism test_daemon.py's real-subprocess
tests already use), then spawns a *second*, separate subprocess that
resumes the exact same run_id - and proves, from the database state
alone, that the already-labeled cluster was reused rather than
relabeled.

Marked slow_cross_process (same category as
tests/test_clustering_cross_process.py - real subprocess spawns, cold
numba/UMAP JIT cost) - skipped by default, run via `--run-slow`.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from jarvis_server.internal.migrate import run_migrations
from jarvis_server.internal import db
from jarvis_server.internal.llm.llm import LLMClient
from jarvis_server.organize import clustering, config_versions
from tests.test_clustering import StubEmbedder, _blobs, _seed_chunks

pytestmark = pytest.mark.slow_cross_process

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKER = Path(__file__).resolve().parent / "_interrupt_resume_worker.py"

SLEEP_SECONDS = 3.0
RUN_ID = "interrupt-resume-test-run"


@pytest.fixture(autouse=True)
def _require_run_slow_flag(request):
    if not request.config.getoption("--run-slow"):
        pytest.skip("slow cross-process test skipped - pass --run-slow to actually spawn subprocesses")


class _MustNotBeCalledLLM(LLMClient):
    """If organize() calls this even once, checkpointing didn't actually
    skip already-labeled clusters - fail loudly, don't silently pass."""

    def complete(self, system, user, *, temperature=0.2, max_tokens=400):
        raise AssertionError(
            "label_cluster() called the LLM for a cluster that should have "
            "hit its checkpoint from an earlier run - resume is broken"
        )


def _wait_for_n_call_lines(log_path: Path, n: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log_path.exists():
            lines = log_path.read_text(errors="replace").splitlines()
            if sum(1 for l in lines if l.startswith("CALL")) >= n:
                return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {n} CALL line(s) in {log_path}")


def _wait_for_done(log_path: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log_path.exists() and "DONE" in log_path.read_text(errors="replace"):
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for DONE in {log_path}")


def test_a_real_interrupted_run_resumes_from_checkpoints_not_from_scratch(tmp_path):
    # --- setup: real db, real corpus, real organize_runs row ---
    db_path = tmp_path / "interrupt.db"
    run_migrations(db_path)
    conn = db.get_connection(db_path)

    # Verified directly through this exact organize()+DB round trip (not
    # just a bare _base_cluster(matrix) call, which reads a different
    # effective row order than get_all_chunk_vectors' ORDER BY chunk_id
    # does): exactly 3 base clusters, 0 roll-up parents (3 < the
    # MIN_BRANCH_FAN_OUT*2=4 needed to even attempt one) - every LLM call
    # this test observes is unambiguously a base-cluster label, nothing
    # from the (deliberately un-checkpointed, see design doc §1b) roll-up.
    matrix, _truth = _blobs(n_clusters=3, per_cluster=6, seed=303)
    _seed_chunks(conn, matrix)

    version_id = config_versions.ensure_config_version(conn)
    db.insert_organize_run(conn, RUN_ID, version_id, "2020-01-01T00:00:00+00:00")

    log1 = tmp_path / "run1.log"
    log2 = tmp_path / "run2.log"

    # --- run 1: spawn for real, let it get partway, then kill it for real ---
    proc1 = subprocess.Popen(
        [sys.executable, str(WORKER), str(db_path), RUN_ID, str(SLEEP_SECONDS), str(log1)],
        cwd=str(REPO_ROOT),
    )
    try:
        # CALL 2 appearing proves CALL 1 fully finished (label_cluster
        # returned, checkpoint committed) before this process is killed -
        # program order within one single-threaded process, not a guess.
        # Generous timeout: cold numba/UMAP JIT compile alone has been
        # observed taking 60-90s on this machine, on top of clustering
        # itself and the first labeling sleep.
        _wait_for_n_call_lines(log1, 2, timeout=180)
        os.kill(proc1.pid, signal.SIGTERM)
        proc1.wait(timeout=10)
    finally:
        if proc1.poll() is None:
            proc1.kill()
            proc1.wait(timeout=10)

    assert "DONE" not in log1.read_text(), "run 1 finished cleanly - the kill didn't actually interrupt it"

    checkpoints_after_run1 = conn.execute(
        "SELECT member_fingerprint, label FROM organize_cluster_checkpoints WHERE run_id = ?",
        (RUN_ID,),
    ).fetchall()
    assert len(checkpoints_after_run1) == 1, (
        f"expected exactly 1 checkpoint after a kill right after CALL 2 started, "
        f"got {len(checkpoints_after_run1)}"
    )
    checkpoint_from_run1 = dict(checkpoints_after_run1[0])

    # --- run 2: a genuinely separate process, resuming the same run_id ---
    proc2 = subprocess.Popen(
        [sys.executable, str(WORKER), str(db_path), RUN_ID, str(SLEEP_SECONDS), str(log2)],
        cwd=str(REPO_ROOT),
    )
    try:
        # Its own fresh cold JIT compile again, plus two labeling sleeps.
        proc2.wait(timeout=240)
    finally:
        if proc2.poll() is None:
            proc2.kill()
            proc2.wait(timeout=10)
    _wait_for_done(log2, timeout=5)

    run2_calls = [l for l in log2.read_text().splitlines() if l.startswith("CALL")]
    assert len(run2_calls) == 2, (
        f"the resumed run should have made exactly 2 fresh LLM calls (the two clusters "
        f"NOT checkpointed by run 1), made {len(run2_calls)} - either it redid the "
        f"checkpointed one, or didn't do all the remaining ones"
    )

    # --- the real proof: run 1's checkpoint survived untouched, and two new ones appeared ---
    checkpoints_after_run2 = {
        row["member_fingerprint"]: row["label"]
        for row in conn.execute(
            "SELECT member_fingerprint, label FROM organize_cluster_checkpoints WHERE run_id = ?",
            (RUN_ID,),
        ).fetchall()
    }
    assert len(checkpoints_after_run2) == 3, "all three real clusters must be checkpointed after resume completes"
    assert checkpoints_after_run2[checkpoint_from_run1["member_fingerprint"]] == checkpoint_from_run1["label"], (
        "run 1's checkpoint was overwritten/relabeled by the resume - it should have been reused untouched"
    )

    # --- the strongest check: a third, fully in-process call with an LLM
    # that raises if it's ever invoked - every fingerprint must hit ---
    result = clustering.organize(conn, StubEmbedder(), _MustNotBeCalledLLM(), run_id=RUN_ID)
    base_nodes = [n for n in result.nodes if n.key.startswith("base-")]
    assert len(base_nodes) == 3
    for n in base_nodes:
        fp = clustering._fingerprint_members(n.member_chunk_ids)
        assert fp in checkpoints_after_run2
        assert n.label == checkpoints_after_run2[fp]

    conn.close()
