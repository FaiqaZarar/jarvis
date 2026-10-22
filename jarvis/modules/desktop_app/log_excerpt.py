"""Best-effort local-log excerpt attached to a feedback submission
(feedback_dialog.py) - lets a vague "it's not working" report come with
whatever the app was actually doing in the minutes right before the user
noticed and clicked Send, instead of nothing at all.

A true "-1/+1 minute centered on the click" isn't achievable - there's no
way to capture log lines that haven't been written yet without delaying
the whole submission by a real minute. This instead captures the
preceding window up to the click, which covers the same debugging value:
whatever went wrong already happened before the user noticed.

Never raises - a missing/unreadable log file degrades to an empty
excerpt, same convention as update_checker.check_for_update's "never
raises" contract, since a logging problem here must never block a
feedback submission.
"""

from datetime import datetime, timedelta

from jarvis.modules.daemon.client import LOG_FILE

# Read only the tail of the file rather than scanning it from the start -
# LOG_FILE is only rotated at startup (logging_config.rotate_if_large), so
# it can still grow over a long-running session, but the window this
# module looks for is always near "now".
_TAIL_READ_BYTES = 256 * 1024
_DEFAULT_WINDOW_MINUTES = 2
_DEFAULT_MAX_CHARS = 8000

# Matches logging.Formatter("%(asctime)s %(levelname)s ...")'s default
# asctime shape - local time (time.localtime()), not UTC, so callers must
# pass a local (naive) `before` to compare correctly against it.
_ASCTIME_FORMAT = "%Y-%m-%d %H:%M:%S,%f"
_ASCTIME_LENGTH = 23  # "YYYY-MM-DD HH:MM:SS,mmm"


def _parse_line_timestamp(line: str):
    try:
        return datetime.strptime(line[:_ASCTIME_LENGTH], _ASCTIME_FORMAT)
    except ValueError:
        return None  # a continuation line (e.g. a wrapped traceback) - no leading timestamp of its own


def get_recent_log_excerpt(
    before: datetime,
    minutes: int = _DEFAULT_WINDOW_MINUTES,
    max_chars: int = _DEFAULT_MAX_CHARS,
) -> str:
    try:
        with open(LOG_FILE, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - _TAIL_READ_BYTES))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""

    window_start = before - timedelta(minutes=minutes)
    lines = tail.splitlines()
    selected = []
    for line in lines:
        ts = _parse_line_timestamp(line)
        if ts is None:
            if selected:
                selected.append(line)  # a continuation of the previous (in-window) line
            continue
        if window_start <= ts <= before:
            selected.append(line)

    excerpt = "\n".join(selected)
    if len(excerpt) > max_chars:
        excerpt = excerpt[-max_chars:]
    return excerpt
