"""Tests for format_utils.format_relative_time (DESIGN.md §5.3 - Figma
parity pass, Phase 9). format_bytes/format_duration are already covered
inline in test_desktop_app_views.py; this file is scoped to the new
helper and tilde_path."""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jarvis.modules.desktop_app import format_utils
from jarvis.modules.desktop_app.format_utils import format_relative_time

_NOW = datetime(2026, 10, 12, 15, 0, 0)


def _iso(delta: timedelta) -> str:
    return (_NOW - delta).isoformat()


def test_just_now_for_under_a_minute():
    assert format_relative_time(_iso(timedelta(seconds=30)), now=_NOW) == "just now"


def test_minutes_ago():
    assert format_relative_time(_iso(timedelta(minutes=5)), now=_NOW) == "5 minutes ago"


def test_one_minute_ago_is_singular():
    assert format_relative_time(_iso(timedelta(minutes=1)), now=_NOW) == "1 minute ago"


def test_hours_ago():
    assert format_relative_time(_iso(timedelta(hours=3)), now=_NOW) == "3 hours ago"


def test_one_hour_ago_is_singular():
    assert format_relative_time(_iso(timedelta(hours=1)), now=_NOW) == "1 hour ago"


def test_yesterday():
    assert format_relative_time(_iso(timedelta(days=1, hours=2)), now=_NOW) == "yesterday"


def test_a_few_days_ago():
    assert format_relative_time(_iso(timedelta(days=3)), now=_NOW) == "3 days ago"


def test_falls_back_to_a_date_past_a_week_same_year():
    assert format_relative_time(_iso(timedelta(days=10)), now=_NOW) == "2 Oct"


def test_falls_back_to_a_date_with_year_across_a_year_boundary():
    assert format_relative_time(_iso(timedelta(days=400)), now=_NOW) == "7 Sep 2025"


def test_utc_timestamp_is_converted_before_comparing():
    # A UTC ISO string (real server shape: datetime.now(timezone.utc).isoformat())
    # must compare against local `now`, not get a bogus multi-hour-offset delta
    # purely from the UTC offset itself.
    local_offset = datetime.now().astimezone().utcoffset()
    utc_ts = (_NOW - timedelta(minutes=5) - local_offset).replace(tzinfo=timezone.utc)
    assert format_relative_time(utc_ts.isoformat(), now=_NOW) == "5 minutes ago"


def test_unparseable_timestamp_falls_back_to_the_raw_string():
    assert format_relative_time("not-a-timestamp", now=_NOW) == "not-a-timestamp"


def test_tilde_path_shortens_the_home_folder():
    home = str(Path.home())

    assert format_utils.tilde_path(os.path.join(home, "Notes", "a.md")) == os.path.join("~", "Notes", "a.md")
    assert format_utils.tilde_path(home) == "~"


def test_tilde_path_leaves_other_paths_and_lookalike_prefixes_alone():
    home = str(Path.home())

    assert format_utils.tilde_path(home + "-old" + os.sep + "a.md") == home + "-old" + os.sep + "a.md"
    assert format_utils.tilde_path(os.sep + "srv" + os.sep + "a.md") == os.sep + "srv" + os.sep + "a.md"
