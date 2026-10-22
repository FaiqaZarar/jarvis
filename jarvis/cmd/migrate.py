"""Entrypoint shim: `python -m jarvis.cmd.migrate` -> jarvis_server.internal.migrate.

The real implementation moved to `jarvis_server.internal.migrate` as part
of physically isolating server-side code from client-side code (the
schema/migration runner is squarely a server responsibility - the daemon
calls it directly, in-process, on every startup via `_preflight()`).
Kept here, thin, because `python -m jarvis.cmd.migrate` is a real,
documented CLI entrypoint (README) independent of the daemon - a
developer inspecting or migrating a specific `.db` file by hand
shouldn't need to know the implementation lives in a different,
separately-packaged distribution now.
"""

import argparse
from pathlib import Path

from jarvis_server.internal.migrate import DEFAULT_DB_PATH, run_migrations

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Jarvis DB migrations.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="Path to the SQLite file")
    args = parser.parse_args()
    run_migrations(args.db)
