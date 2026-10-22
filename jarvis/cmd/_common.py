"""Shared CLI helpers for the ingest entrypoints (drive_ingest.py,
filesystem_ingest.py). Not a runnable module itself."""

import sys


def confirm(prompt: str) -> bool:
    return input(f"{prompt} [Y/n]: ").strip().lower() in ("", "y", "yes")


def print_ingest_timeout_hint(exc: Exception) -> None:
    """A DaemonTimeoutError from an ingest call (run_ingest_filesystem/
    run_ingest_drive - see daemon/client.py's `_run_ingest`) most likely means the ingest is still running, not
    that anything failed - this CLI process just stopped waiting for it.
    Confirmed as a real scenario, not a hypothetical: a genuine 19-file
    Drive folder outlived the old shared 120s read timeout while the
    daemon kept working correctly in the background. Restarting the
    daemon/app now would kill real in-progress work - the opposite of
    what DaemonConnectionError's generic "try restarting" advice implies,
    which is why this gets its own message rather than reusing it."""
    print(
        f"\n{exc}\n\n"
        "This can happen on a large folder or a full hierarchy rebuild - the ingest "
        "may still be running in the background even though this command stopped "
        "waiting for it. Don't restart the daemon/app while that's possible: check "
        "`curl http://127.0.0.1:<port>/ingest/status` (port is in .jarvis_daemon.json) "
        "or just wait and try again shortly.",
        file=sys.stderr,
    )


def print_ingest_summary(result: dict) -> None:
    """Render a `pipeline.run_ingest` result dict - same shape regardless
    of which connector produced it."""
    sync = result["sync"]
    print("\nDone.")
    print(
        f"  pulled {sync['pulled']}  "
        f"(fast-skipped {sync['fast_skipped']})  "
        f"skipped {sync['skipped']}  errors {sync['errors']}"
    )
    print(f"  chunks created:  {result['chunks_created']}")
    embed = result.get("embed", {})
    if embed:
        print(f"  newly embedded:  {embed.get('newly_embedded', '?')}")
    hierarchy = result.get("hierarchy", {})
    if "skipped" in hierarchy:
        print(f"  topic hierarchy: skipped ({hierarchy['skipped']})")
    else:
        rec = hierarchy.get("reconcile", {})
        print(
            f"  topic hierarchy: {hierarchy.get('organize', {}).get('topics', '?')} topics, "
            f"{rec.get('born', 0)} new / {rec.get('tombstoned', 0)} retired"
        )
    if sync["errors"]:
        print(
            f"\n  {sync['errors']} file(s) errored - see the log above. They'll be "
            "retried on the next run."
        )
