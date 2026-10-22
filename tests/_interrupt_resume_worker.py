"""Standalone worker for test_organize_checkpointing_interrupt.py.

Not a test module itself (not named test_*.py, so pytest never collects
it) - a real, separate `python` subprocess runs this, exactly the same
methodology tests/_cross_process_determinism_worker.py uses. This one
runs `clustering.organize()` against a real, already-migrated,
already-seeded sqlite file (passed in via argv, set up once by the
parent test before spawning either subprocess) - and, critically, uses
an LLM stub that logs each call to a file *before* sleeping, so an
external watcher can observe real partial progress and kill this
process mid-loop, then verify from the log exactly how far it got.

argv: db_path run_id sleep_seconds log_path
"""

import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from jarvis_server.internal import db  # noqa: E402
from jarvis_server.internal.llm.llm import LLMClient  # noqa: E402
from jarvis_server.organize import clustering  # noqa: E402
from tests.test_clustering import StubEmbedder  # noqa: E402


class SlowLoggingLLM(LLMClient):
    """Appends one line to `log_path` - flushed immediately - *before*
    sleeping, so a call that's interrupted mid-sleep (by this whole
    process being killed) still leaves real evidence it started. A clean
    finish appends a whole extra "DONE" line the parent can check for."""

    def __init__(self, log_path: Path, sleep_seconds: float):
        self._log_path = log_path
        self._sleep_seconds = sleep_seconds
        self._i = 0

    def complete(self, system, user, *, temperature=0.2, max_tokens=400):
        self._i += 1
        with open(self._log_path, "a", encoding="utf-8") as fh:
            fh.write(f"CALL {self._i}\n")
            fh.flush()
        time.sleep(self._sleep_seconds)
        return json.dumps({"title": f"Topic {self._i}", "description": f"desc {self._i}"})


def main() -> None:
    db_path, run_id, sleep_seconds, log_path = sys.argv[1:5]
    sleep_seconds = float(sleep_seconds)
    log_path = Path(log_path)

    conn = db.get_connection(db_path)
    llm = SlowLoggingLLM(log_path, sleep_seconds)
    clustering.organize(conn, StubEmbedder(), llm, run_id=run_id)

    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write("DONE\n")
        fh.flush()


if __name__ == "__main__":
    main()
