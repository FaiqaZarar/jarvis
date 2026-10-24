"""Checks the project's GitHub Releases for a newer build than this one.

Desktop builds are published as releases of the public repo
(`_RELEASES_REPO` below) by `.github/workflows/build.yml`; this module
polls it unauthenticated. **Keep `_RELEASES_REPO` in sync with
the repo `build.yml` runs in** - releases are published there.

Release tags are `vMAJOR.MINOR.PATCH`, optionally with a pre-release
suffix (`v0.1.0-preview`); `_version_key()` orders them numerically, with
a final release above its own pre-releases. A build that is itself a
pre-release is offered newer pre-releases too; a final build only ever
sees final releases. A tag outside that scheme is never offered.

Each release carries one asset per platform (`build.yml` uploads both
`Jarvis.dmg` and `JarvisSetup.exe`) - `_asset_suffix_for_platform()`
picks the one this process can actually install, matching `sys.platform`
against a suffix, so a Mac build never gets offered the Windows
installer or vice versa.

`check_for_update()` never raises - a network failure, a non-2xx
response, malformed JSON, an unsupported platform, or a release with no
matching asset all just mean "no update found," matching the decision
that the automatic, on-launch check stays completely silent on failure.
"""

import re
import sys
from dataclasses import dataclass
from typing import Any, Callable, Optional

import requests
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

_RELEASES_REPO = "FaiqaZarar/jarvis"  # the repo build.yml runs in and releases to
_RELEASES_URL = f"https://api.github.com/repos/{_RELEASES_REPO}/releases?per_page=20"
_REQUEST_TIMEOUT_S = 5.0
# GitHub's API returns 403 for an anonymous request with no User-Agent at all.
_USER_AGENT = "jarvis-desktop-updater"


def releases_page_url() -> str:
    """The human-facing releases page - "Download manually" opens this
    in a browser when an automatic install fails (DESIGN.md §6)."""
    return f"https://github.com/{_RELEASES_REPO}/releases"


@dataclass(frozen=True)
class UpdateInfo:
    tag: str
    download_url: str
    html_url: str
    # The release asset's own filename (e.g. "Jarvis.dmg", "JarvisSetup.exe") -
    # update_downloader.py needs this to save the download under the right
    # extension, since download_url itself is a GitHub-redirected URL, not
    # necessarily one ending in a real filename. Defaults to "" so existing
    # callers/tests that construct UpdateInfo directly (not through
    # check_for_update) don't need updating.
    asset_name: str = ""


def _asset_suffix_for_platform() -> Optional[str]:
    if sys.platform == "darwin":
        return ".dmg"
    if sys.platform == "win32":
        return ".exe"
    return None


def _version_key(tag: Any) -> Optional[tuple]:
    """Sort key for `v1.2.3` / `v1.2.3-preview`, or None for any other tag."""
    if not isinstance(tag, str):
        return None
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?", tag)
    if not m:
        return None
    major, minor, patch, pre = m.groups()
    return (int(major), int(minor), int(patch)), pre is None, pre or ""


def _matching_asset(release: dict, suffix: str) -> Optional[dict]:
    assets = release.get("assets") or []
    asset = next(
        (a for a in assets if isinstance(a, dict) and str(a.get("name", "")).endswith(suffix)), None,
    )
    if asset is None or not asset.get("browser_download_url"):
        return None
    return asset


def check_for_update(current_version: Optional[str]) -> Optional[UpdateInfo]:
    current_key = _version_key(current_version)
    if current_key is None:
        return None
    on_prerelease = not current_key[1]

    suffix = _asset_suffix_for_platform()
    if suffix is None:
        return None

    try:
        resp = requests.get(
            _RELEASES_URL,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"},
            timeout=_REQUEST_TIMEOUT_S,
        )
        resp.raise_for_status()
        releases = resp.json()
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(releases, list):
        return None

    best = None
    for release in releases:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        if release.get("prerelease") and not on_prerelease:
            continue
        key = _version_key(release.get("tag_name"))
        if key is None or key <= current_key:
            continue
        asset = _matching_asset(release, suffix)
        if asset is not None and (best is None or key > best[0]):
            best = (key, release, asset)
    if best is None:
        return None

    _, release, asset = best
    return UpdateInfo(
        tag=release["tag_name"],
        download_url=asset["browser_download_url"],
        html_url=str(release.get("html_url", "")),
        asset_name=str(asset.get("name", "")),
    )


class _UpdateCheckSignals(QObject):
    finished = Signal(object)  # Optional[UpdateInfo] - this never fails "loudly", see module docstring


class UpdateCheckWorker(QRunnable):
    def __init__(self, current_version: Optional[str]):
        super().__init__()
        self._current_version = current_version
        self.signals = _UpdateCheckSignals()

    def run(self) -> None:
        self.signals.finished.emit(check_for_update(self._current_version))


# Same lifetime issue daemon_worker.py's _in_flight fixes: QThreadPool.start()
# only keeps the C++ QRunnable alive, not this Python wrapper (or its
# `signals` QObject, where the actual connection lives) - a caller that
# doesn't hold the return value could have it garbage-collected before the
# pool thread ever runs it.
_in_flight: set[UpdateCheckWorker] = set()


def run_update_check(
    pool: QThreadPool,
    current_version: Optional[str],
    on_result: Callable[[Optional[UpdateInfo]], None],
) -> UpdateCheckWorker:
    worker = UpdateCheckWorker(current_version)
    _in_flight.add(worker)

    def _handle(result: Any) -> None:
        _in_flight.discard(worker)
        on_result(result)

    worker.signals.finished.connect(_handle)
    pool.start(worker)
    return worker
