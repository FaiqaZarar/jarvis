"""The packaged desktop app's own build version.

No version identifier existed anywhere in this repo before this module -
`.github/workflows/build.yml` computes a release tag
(`desktop-build-YYYY-MM-DD[-runN]`) but never embedded it into the built
binary. `build-macos` now writes that exact tag into a plain `VERSION`
file (`packaging/jarvis_desktop_mac.spec`'s own `datas` entry)
before invoking PyInstaller, landing it at the root of `sys._MEIPASS` -
so the running app can read back the literal tag its own build was
published under, with no separate computation that could drift from it.

`None` outside a frozen build (dev has no `_MEIPASS` and no meaningful
"my build's tag" at all) and `None` if the file is missing for any
reason (e.g. a manual local PyInstaller run that skipped the CI stamping
step) - a safe fallback, not a crash. `update_checker.check_for_update`
treats `None` as "never check for updates," so a dev build silently never
talks to the update endpoint.
"""

import sys
from pathlib import Path
from typing import Optional


def get_current_version() -> Optional[str]:
    if not getattr(sys, "frozen", False):
        return None
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return None
    try:
        text = (Path(meipass) / "VERSION").read_text()
    except OSError:
        return None
    return text.strip() or None
