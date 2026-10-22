"""Ingest one local folder into Jarvis.

    python -m jarvis.cmd.filesystem_ingest /path/to/a/folder

Sends the folder to the daemon, which does the actual pull -> chunk ->
embed -> organize (`pipeline.run_ingest`, design doc §4 - the daemon
owns "the actual processing", not just the connection).

No migration step here anymore (server/client isolation, resolved
entanglement point #2): this used to also run migrations against a
`--db`-selected file before talking to the daemon, but that was already
disconnected from where ingest data actually lands - the flag's own old
help text said as much ("does not change where ingested content ends
up"), since the daemon is the sole process that opens `jarvis.db` and
already auto-migrates it, unconditionally, on every startup
(`_preflight()` in `jarvis_server.daemon.server`). Migrating a *different*
file by hand is still possible via the real, standalone entrypoint this
was always redundant with: `python -m jarvis.cmd.migrate --db path.db`.

Flags:
  --no-organize    stop after indexing; skip the topic-hierarchy step
                   (faster, and no LLM API cost)
  --yes            skip the confirmation prompt
"""

import argparse
import sys

from dotenv import load_dotenv

# Load <repo>/.env before importing daemon_client (it reads
# JARVIS_DAEMON_HOME/JARVIS_DB_PATH at import time). Same rationale as
# drive_ingest.py / mcp_server.py.
load_dotenv()

from jarvis.cmd._common import confirm, print_ingest_summary, print_ingest_timeout_hint  # noqa: E402
from jarvis_server.internal.logging.logging_config import get_logger  # noqa: E402
from jarvis.modules.daemon import client as daemon_client  # noqa: E402
from jarvis.modules.daemon.client import DaemonConnectionError, DaemonTimeoutError  # noqa: E402

logger = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m jarvis.cmd.filesystem_ingest",
        description="Ingest one local folder into Jarvis.",
    )
    parser.add_argument("path", help="folder to ingest")
    parser.add_argument(
        "--no-organize", action="store_true",
        help="stop after indexing; skip the topic-hierarchy step (no LLM cost)",
    )
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args(argv)

    try:
        if not args.yes and not confirm(
            f'\nIngest "{args.path}"? This reads and indexes its files'
        ):
            print("Cancelled.")
            return 0

        print(f'\nIngesting "{args.path}"...')
        result = daemon_client.run_ingest_filesystem(
            args.path, organize_hierarchy=not args.no_organize,
        )
        if "error" in result:
            print(f"\n{result['error']['message']}", file=sys.stderr)
            return 1
        print_ingest_summary(result)
        return 0

    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except DaemonTimeoutError as exc:
        # Tier 3, ingest-specific: the daemon stopped answering while
        # starting or checking on the run, which doesn't mean the run
        # itself failed - see _common.print_ingest_timeout_hint.
        print_ingest_timeout_hint(exc)
        return 1
    except DaemonConnectionError as exc:
        # Tier 2: the daemon was reachable a moment ago (or auto-start
        # succeeded) but the connection itself failed - this one really
        # is "something's wrong with the daemon process."
        print(f"\n{exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        # Tier 1 (DaemonStartupError, a RuntimeError subclass - see
        # daemon/client.py) plus our own pre-existing clear messages,
        # e.g. a missing/wrong EMBEDDING_API_KEY.
        print(f"\n{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - last-resort, keep it readable
        logger.exception("filesystem_ingest failed")
        print(f"\nSomething went wrong: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
