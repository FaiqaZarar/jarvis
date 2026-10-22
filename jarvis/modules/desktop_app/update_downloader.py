"""Downloads the release asset an `UpdateInfo` points at (a Mac `.dmg` or
a Windows `.exe` installer, whichever `update_checker.py` matched for
this platform), off the GUI thread.

Streamed via plain `requests` (the only HTTP library already used
anywhere in this codebase - `jarvis.modules.daemon.client`,
`jarvis_server.internal.embed.provider`, `.../llm/llm.py`) rather than
anything browser-based - a real, load-bearing distinction on macOS: a
browser download is what tags a file with macOS's `com.apple.quarantine`
extended attribute in the first place, and a plain socket-level download
like this one never does. So the file this module produces needs no
quarantine handling at all; `update_installer.py`'s defensive `xattr -cr`
on the *mounted* `.app` is insurance against something else re-tagging
it, not a step this module's own output requires.

Written to `<daemon_home>/updates/Jarvis-<tag><ext>.part` first, then
`Path.replace()`'d to the final name - an atomic rename, so a crash or
kill mid-download never leaves a half-written file sitting at the name
`update_installer.py` will later trust and run.
"""

from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import requests
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from jarvis.modules.desktop_app.update_checker import UpdateInfo

_CHUNK_SIZE = 256 * 1024
_DOWNLOAD_TIMEOUT_S = 30.0
# Only hit if asset_name is somehow blank (e.g. an UpdateInfo built by
# hand, not through check_for_update, or a URL with a bare filename) -
# check_for_update always populates a real suffix in practice.
_FALLBACK_SUFFIX = ".bin"


def _asset_suffix(update_info: UpdateInfo) -> str:
    if update_info.asset_name:
        suffix = Path(update_info.asset_name).suffix
        if suffix:
            return suffix
    suffix = Path(urlparse(update_info.download_url).path).suffix
    return suffix or _FALLBACK_SUFFIX


def download_update(
    update_info: UpdateInfo,
    daemon_home: Path,
    on_progress: Optional[Callable[[int, Optional[int]], None]] = None,
) -> Path:
    updates_dir = daemon_home / "updates"
    updates_dir.mkdir(parents=True, exist_ok=True)
    final_path = updates_dir / f"Jarvis-{update_info.tag}{_asset_suffix(update_info)}"
    part_path = updates_dir / (final_path.name + ".part")

    resp = requests.get(update_info.download_url, stream=True, timeout=_DOWNLOAD_TIMEOUT_S)
    resp.raise_for_status()
    content_length = resp.headers.get("Content-Length")
    total_bytes = int(content_length) if content_length is not None else None

    downloaded = 0
    with open(part_path, "wb") as fh:
        for chunk in resp.iter_content(chunk_size=_CHUNK_SIZE):
            if not chunk:
                continue
            fh.write(chunk)
            downloaded += len(chunk)
            if on_progress is not None:
                on_progress(downloaded, total_bytes)

    part_path.replace(final_path)
    return final_path


class _DownloadSignals(QObject):
    progress = Signal(int, object)  # (downloaded, total_bytes_or_None)
    succeeded = Signal(object)  # Path
    failed = Signal(object)  # the exception instance


class DownloadWorker(QRunnable):
    def __init__(self, update_info: UpdateInfo, daemon_home: Path):
        super().__init__()
        self._update_info = update_info
        self._daemon_home = daemon_home
        self.signals = _DownloadSignals()

    def run(self) -> None:
        try:
            path = download_update(
                self._update_info,
                self._daemon_home,
                on_progress=lambda downloaded, total: self.signals.progress.emit(downloaded, total),
            )
        except Exception as exc:  # noqa: BLE001 - see daemon_worker.py's identical rationale
            self.signals.failed.emit(exc)
            return
        self.signals.succeeded.emit(path)


# Same _in_flight pattern as daemon_worker.py/update_checker.py - see
# either module's docstring for the GC bug this guards against.
_in_flight: set[DownloadWorker] = set()


def run_update_download(
    pool: QThreadPool,
    update_info: UpdateInfo,
    daemon_home: Path,
    *,
    on_progress: Optional[Callable[[int, Optional[int]], None]] = None,
    on_success: Optional[Callable[[Path], None]] = None,
    on_error: Optional[Callable[[Exception], None]] = None,
) -> DownloadWorker:
    worker = DownloadWorker(update_info, daemon_home)
    _in_flight.add(worker)

    def _cleanup(_result: object = None) -> None:
        _in_flight.discard(worker)

    if on_progress is not None:
        worker.signals.progress.connect(on_progress)
    if on_success is not None:
        worker.signals.succeeded.connect(on_success)
    if on_error is not None:
        worker.signals.failed.connect(on_error)
    worker.signals.succeeded.connect(_cleanup)
    worker.signals.failed.connect(_cleanup)

    pool.start(worker)
    return worker
