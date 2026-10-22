"""Ingest one Google Drive folder into Jarvis - interactive CLI.

    python -m jarvis.cmd.drive_ingest

What it does, in order:
  1. Signs you in to Google Drive. First run (and roughly weekly after,
     because Testing-mode tokens expire in ~7 days) opens a browser; other
     runs reuse the cached token silently.
  2. Lists your Drive folders and lets you pick one by number - open a
     sub-folder with its number, pick the current folder with 0, go back
     with "b". No copying folder ids out of browser URLs.
  3. Sends the folder to the daemon, which does the actual pull -> chunk
     -> embed -> organize (`pipeline.run_ingest`, design doc §4 - the
     daemon owns "the actual processing", not just the connection).
     OAuth sign-in and folder listing/picking above stay local to this
     CLI (design doc's CLI-conversion plan, Phase A) - only the ingest
     call itself is daemon-routed for now.

Flags:
  --folder-id ID   skip the picker and use this folder id directly
  --no-organize    stop after indexing; skip the topic-hierarchy step
                   (faster, and no LLM API cost)

No migration step here anymore (server/client isolation, resolved
entanglement point #2) - see filesystem_ingest.py's module docstring for
the full reasoning; the short version is the daemon already auto-
migrates its own real db unconditionally on every startup, so this was
redundant with that.

**Deliberate, acknowledged exception to the client/server import
boundary (resolved entanglement point #1)**: this file imports
`jarvis_server.connectors.drive` directly, for interactive OAuth sign-in
and folder listing/picking - NOT routed through the daemon's HTTP API,
unlike the actual ingest call below. This is intentional, not an
oversight: OAuth consent has to open a browser and block for the user in
the *foreground* of whatever process they're running, which a background
daemon has no way to do on a caller's behalf. Pushing this behind a new
daemon endpoint would mean the daemon itself owns Google OAuth token
storage/refresh - real scope creep in the wrong direction, not cleaner
isolation. `jarvis_server.connectors.drive`'s auth/listing pieces are
treated as part of the server distribution's public interface, the same
way `daemon.client` is - client code is expected to depend on this
specific slice of it for this specific, interactive-by-nature reason.
"""

import argparse
import sys

from dotenv import load_dotenv

# Load <repo>/.env before importing daemon_client / drive (they read
# JARVIS_DAEMON_HOME / the OAuth vars at construction time). Same
# rationale as mcp_server.py.
load_dotenv()

from jarvis.cmd._common import confirm, print_ingest_summary, print_ingest_timeout_hint  # noqa: E402
from jarvis_server.internal.logging.logging_config import get_logger  # noqa: E402
from jarvis_server.connectors import drive  # noqa: E402
from jarvis.modules.daemon import client as daemon_client  # noqa: E402
from jarvis.modules.daemon.client import DaemonConnectionError, DaemonTimeoutError  # noqa: E402

logger = get_logger(__name__)

_ROOT = "root"
_ROOT_LABEL = "My Drive"


def _rate_limit_aware(fn, *args, **kwargs):
    """Call a drive.* listing helper; turn a rate-limit HttpError into a
    clear RuntimeError (main() prints it without a traceback)."""
    from googleapiclient.errors import HttpError

    try:
        return fn(*args, **kwargs)
    except HttpError as exc:
        if drive.is_rate_limited(exc):
            raise RuntimeError(
                "Google is rate-limiting Drive requests right now. "
                "Wait a moment and run this again."
            ) from exc
        raise


def _list_subfolders(service, parent_id: str) -> list[dict]:
    return _rate_limit_aware(drive.list_drive_folders, service, parent_id=parent_id)


def pick_folder(service) -> tuple[str, str]:
    """Interactive folder picker. Returns ``(folder_id, folder_name)``.

    Uses the module-level ``input`` / ``print`` so tests drive it by
    monkeypatching ``builtins.input``.
    """
    # each entry: (folder_id, display_name); stack[-1] is the current location
    stack: list[tuple[str, str]] = [(_ROOT, _ROOT_LABEL)]

    while True:
        folder_id, name = stack[-1]
        subs = _list_subfolders(service, folder_id)

        print(f"\nLocation: {' / '.join(n for _, n in stack)}")
        if subs:
            for i, f in enumerate(subs, start=1):
                print(f"  {i}. {f['name']}")
        else:
            print("  (no sub-folders here)")

        prompt = f'Enter a number to open a sub-folder, 0 to ingest "{name}"'
        if len(stack) > 1:
            prompt += ", b to go back"
        prompt += ": "

        raw = input(prompt).strip().lower()

        if raw == "b" and len(stack) > 1:
            stack.pop()
            continue
        if not raw.isdigit():
            print("  Please enter a number (or b to go back).")
            continue

        choice = int(raw)
        if choice == 0:
            # A second walk (the ingest does its own), so the user sees the
            # real sub-tree scope before confirming, not just direct children.
            print(f'\nScanning "{name}" and its sub-folders...')
            found = _rate_limit_aware(drive.list_drive_files_recursive, service, folder_id)
            ingestable = sum(
                1 for f in found if f.get("mimeType") in drive.INGESTABLE_MIMETYPES
            )
            print(
                f"  {len(found)} item(s) found, {ingestable} directly ingestable "
                f"(Google Docs, Word, PDF)."
            )
            return folder_id, name
        if 1 <= choice <= len(subs):
            stack.append((subs[choice - 1]["id"], subs[choice - 1]["name"]))
            continue
        print(f"  Enter a number between 0 and {len(subs)}.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m jarvis.cmd.drive_ingest",
        description="Ingest one Google Drive folder into Jarvis.",
    )
    parser.add_argument("--folder-id", default=None, help="skip the picker, use this folder id")
    parser.add_argument(
        "--no-organize", action="store_true",
        help="stop after indexing; skip the topic-hierarchy step (no LLM cost)",
    )
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args(argv)

    try:
        service = drive.get_drive_service()

        if args.folder_id:
            folder_id, folder_name = args.folder_id, args.folder_id
        else:
            folder_id, folder_name = pick_folder(service)

        if not (args.yes or args.folder_id) and not confirm(
            f'\nIngest "{folder_name}"? This downloads and indexes its files'
        ):
            print("Cancelled.")
            return 0

        print(f'\nIngesting "{folder_name}"...')
        result = daemon_client.run_ingest_drive(
            folder_id, organize_hierarchy=not args.no_organize,
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
        # Tier 3, ingest-specific - see filesystem_ingest.py's identical
        # except block.
        print_ingest_timeout_hint(exc)
        return 1
    except DaemonConnectionError as exc:
        # Tier 2: the connection itself failed, not just a slow ingest -
        # this one really does mean something's wrong with the daemon.
        print(f"\n{exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        # Tier 1 (DaemonStartupError, a RuntimeError subclass) plus our
        # own pre-existing clear messages: missing OAuth creds,
        # non-interactive auth, rate-limit on the listing.
        print(f"\n{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - last-resort, keep it readable
        logger.exception("drive_ingest failed")
        print(f"\nSomething went wrong: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
