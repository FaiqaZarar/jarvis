"""Checks the project's GitHub Releases for a newer build than this one.

Desktop builds are published as releases of the public repo
(`_RELEASES_REPO` below) by `.github/workflows/build.yml`; this module
polls it unauthenticated. **Keep `_RELEASES_REPO` in sync with
`build.yml`'s own `RELEASES_REPO` env var** - nothing enforces the two
match automatically.

Version comparison is plain string inequality on the release tag, not
semver - matching the existing non-semver `desktop-build-YYYY-MM-DD[-runN]`
scheme (`build.yml`'s `compute-tag` job), which already sorts correctly
lexicographically except for one known, accepted gap: a same-day 10th+
release breaks ordering (`"...-run10" < "...-run9"` as strings). Cheap
fix if that ever actually happens is zero-padding the run suffix in
`build.yml`, not reaching for semver for a scheme that isn't semver.

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

import sys
from dataclasses import dataclass
from typing import Any, Callable, Optional

import requests
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

_RELEASES_REPO = "FaiqaZarar/jarvis"  # keep in sync with build.yml's RELEASES_REPO
_LATEST_RELEASE_URL = f"https://api.github.com/repos/{_RELEASES_REPO}/releases/latest"
_REQUEST_TIMEOUT_S = 5.0
# GitHub's API returns 403 for an anonymous request with no User-Agent at all.
_USER_AGENT = "jarvis-desktop-updater"


def releases_page_url() -> str:
    """The human-facing releases page - "Download manually" opens this
    in a browser when an automatic install fails (DESIGN.md §6)."""
    return f"https://github.com/{_RELEASES_REPO}/releases/latest"


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


def check_for_update(current_version: Optional[str]) -> Optional[UpdateInfo]:
    if not current_version:
        return None

    suffix = _asset_suffix_for_platform()
    if suffix is None:
        return None

    try:
        resp = requests.get(
            _LATEST_RELEASE_URL,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"},
            timeout=_REQUEST_TIMEOUT_S,
        )
        resp.raise_for_status()
        body = resp.json()
    except (requests.RequestException, ValueError):
        return None

    tag = body.get("tag_name")
    if not tag or not isinstance(tag, str) or tag <= current_version:
        return None

    assets = body.get("assets") or []
    matched_asset = next(
        (a for a in assets if isinstance(a, dict) and str(a.get("name", "")).endswith(suffix)), None,
    )
    if matched_asset is None or not matched_asset.get("browser_download_url"):
        return None

    return UpdateInfo(
        tag=tag,
        download_url=matched_asset["browser_download_url"],
        html_url=str(body.get("html_url", "")),
        asset_name=str(matched_asset.get("name", "")),
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
