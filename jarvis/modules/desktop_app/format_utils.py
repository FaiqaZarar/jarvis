"""Small display-formatting helpers shared by the Settings and Stats &
Activity views - factored out here rather than duplicated in both, since
neither view owns the other's data."""

import os
from datetime import datetime
from pathlib import Path
from typing import Optional


def tilde_path(path: str) -> str:
    """`path` with the home folder shortened to `~`, so Sources and Search
    show the same place the same way."""
    home = str(Path.home())
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def format_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def format_duration(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def format_relative_time(iso_timestamp: str, *, now: Optional[datetime] = None) -> str:
    """DESIGN.md §5.3: relative up to a few days, then a date - matches
    the real Figma reference's own examples ("yesterday", "3 days ago",
    "Oct 12"), which go further than §5.3's literal "up to a day" text
    (that wording gets a follow-up correction, not this function).
    Server timestamps are UTC `datetime.now(timezone.utc).isoformat()`
    strings (confirmed throughout `jarvis_server`) - converted to local
    time before comparing, since "yesterday" means the viewer's own
    clock, not UTC's. Falls back to returning the raw string unchanged
    for anything `fromisoformat` can't parse, rather than raising -
    never worth crashing a view over a display nicety."""
    try:
        ts = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return iso_timestamp
    if ts.tzinfo is not None:
        ts = ts.astimezone().replace(tzinfo=None)
    reference = now if now is not None else datetime.now()
    seconds = (reference - ts).total_seconds()

    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = int(seconds // 3600)
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = int(seconds // 86400)
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    # day-of-month with no leading zero, portably - %-d/%#d are platform-
    # specific strftime flags (glibc vs. MSVC); plain int sidesteps both.
    month = ts.strftime("%b")
    return f"{ts.day} {month}" if ts.year == reference.year else f"{ts.day} {month} {ts.year}"
