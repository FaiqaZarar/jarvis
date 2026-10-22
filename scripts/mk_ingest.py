"""`make ingest FOLDER=...` helper.

Runs the same one-call pipeline the README's "Running it" section shows -
sync -> chunk -> embed -> index -> organize -> reconcile - against the
folder passed as argv[1], and prints the stats dict.

Kept as a file rather than an inline `python -c` so the Makefile recipe
stays readable and this stays testable on its own.
"""

import json
import sys
from pathlib import Path

# `jarvis` is not pip-installed and this script lives in scripts/, so the
# repo root is not on sys.path when run as `python scripts/mk_ingest.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis_server.internal import db  # noqa: E402
from jarvis_server.connectors.filesystem import FilesystemConnector  # noqa: E402
from jarvis_server.pipeline import run_ingest  # noqa: E402  (triggers load_dotenv())


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        print("usage: python scripts/mk_ingest.py <folder>", file=sys.stderr)
        return 2

    folder = Path(sys.argv[1]).expanduser()
    if not folder.is_dir():
        print(f"not a folder: {folder}", file=sys.stderr)
        return 1

    conn = db.get_connection()  # opens <repo>/jarvis.db
    try:
        result = run_ingest(conn, FilesystemConnector(str(folder)))
    finally:
        conn.close()

    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
