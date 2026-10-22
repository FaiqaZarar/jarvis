"""jarvis.modules.desktop_app.update_downloader - the atomic
.part-then-rename download, progress reporting, and the DownloadWorker's
signal delivery (same _in_flight lifetime pattern as daemon_worker.py/
update_checker.py)."""

from PySide6.QtCore import QThreadPool

from jarvis.modules.desktop_app import update_downloader
from jarvis.modules.desktop_app.update_checker import UpdateInfo


class _FakeStreamedResponse:
    def __init__(self, chunks, *, content_length=None):
        self._chunks = chunks
        self.headers = {} if content_length is None else {"Content-Length": str(content_length)}

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        for chunk in self._chunks:
            yield chunk


def _update_info(tag="desktop-build-2026-02-01", **kwargs):
    kwargs.setdefault("download_url", "https://example.com/Jarvis.dmg")
    kwargs.setdefault("html_url", "")
    return UpdateInfo(tag=tag, **kwargs)


def test_download_writes_the_final_file_with_the_full_content(monkeypatch, tmp_path):
    chunks = [b"hello ", b"world"]
    monkeypatch.setattr(
        update_downloader.requests, "get",
        lambda *a, **k: _FakeStreamedResponse(chunks, content_length=11),
    )

    result_path = update_downloader.download_update(_update_info(), tmp_path)

    assert result_path == tmp_path / "updates" / "Jarvis-desktop-build-2026-02-01.dmg"
    assert result_path.read_bytes() == b"hello world"
    assert not (tmp_path / "updates" / "Jarvis-desktop-build-2026-02-01.dmg.part").exists()


def test_download_reports_progress_in_order(monkeypatch, tmp_path):
    chunks = [b"a" * 10, b"b" * 20]
    monkeypatch.setattr(
        update_downloader.requests, "get",
        lambda *a, **k: _FakeStreamedResponse(chunks, content_length=30),
    )
    seen = []

    update_downloader.download_update(_update_info(), tmp_path, on_progress=lambda d, t: seen.append((d, t)))

    assert seen == [(10, 30), (30, 30)]


def test_download_falls_back_to_none_total_when_no_content_length(monkeypatch, tmp_path):
    monkeypatch.setattr(
        update_downloader.requests, "get",
        lambda *a, **k: _FakeStreamedResponse([b"data"]),
    )
    seen = []

    update_downloader.download_update(_update_info(), tmp_path, on_progress=lambda d, t: seen.append((d, t)))

    assert seen == [(4, None)]


def test_download_worker_delivers_success_via_signal(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(
        update_downloader.requests, "get",
        lambda *a, **k: _FakeStreamedResponse([b"x"], content_length=1),
    )

    pool = QThreadPool()
    received = {}
    update_downloader.run_update_download(
        pool, _update_info(), tmp_path, on_success=lambda p: received.setdefault("path", p),
    )

    qtbot.waitUntil(lambda: "path" in received, timeout=2000)
    assert received["path"].name == "Jarvis-desktop-build-2026-02-01.dmg"


def test_download_worker_delivers_failure_via_signal(qtbot, monkeypatch, tmp_path):
    import requests

    def _boom(*a, **k):
        raise requests.ConnectionError("no network")

    monkeypatch.setattr(update_downloader.requests, "get", _boom)

    pool = QThreadPool()
    received = []
    update_downloader.run_update_download(pool, _update_info(), tmp_path, on_error=received.append)

    qtbot.waitUntil(lambda: len(received) == 1, timeout=2000)
    assert isinstance(received[0], requests.ConnectionError)


# ---------------------------------------------------------------------
# _asset_suffix - which extension the saved file gets, on either
# platform's asset (a .dmg or a .exe are just data to this module, same
# streaming logic either way).
# ---------------------------------------------------------------------

def test_download_saves_with_the_exe_extension_from_asset_name(monkeypatch, tmp_path):
    monkeypatch.setattr(
        update_downloader.requests, "get",
        lambda *a, **k: _FakeStreamedResponse([b"x"], content_length=1),
    )
    info = _update_info(
        download_url="https://github.com/x/releases/download/tag/JarvisSetup.exe",
        asset_name="JarvisSetup.exe",
    )

    result_path = update_downloader.download_update(info, tmp_path)

    assert result_path == tmp_path / "updates" / "Jarvis-desktop-build-2026-02-01.exe"


def test_download_falls_back_to_the_url_suffix_when_asset_name_is_blank(monkeypatch, tmp_path):
    monkeypatch.setattr(
        update_downloader.requests, "get",
        lambda *a, **k: _FakeStreamedResponse([b"x"], content_length=1),
    )
    info = _update_info(download_url="https://example.com/JarvisSetup.exe")  # asset_name left blank

    result_path = update_downloader.download_update(info, tmp_path)

    assert result_path == tmp_path / "updates" / "Jarvis-desktop-build-2026-02-01.exe"


def test_download_falls_back_to_bin_when_nothing_gives_a_suffix(monkeypatch, tmp_path):
    monkeypatch.setattr(
        update_downloader.requests, "get",
        lambda *a, **k: _FakeStreamedResponse([b"x"], content_length=1),
    )
    info = _update_info(download_url="https://example.com/download")  # no extension anywhere

    result_path = update_downloader.download_update(info, tmp_path)

    assert result_path == tmp_path / "updates" / "Jarvis-desktop-build-2026-02-01.bin"


def test_download_worker_reports_progress_via_signal(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(
        update_downloader.requests, "get",
        lambda *a, **k: _FakeStreamedResponse([b"a" * 5, b"b" * 5], content_length=10),
    )

    pool = QThreadPool()
    worker = update_downloader.DownloadWorker(_update_info(), tmp_path)
    update_downloader._in_flight.add(worker)

    with qtbot.waitSignal(worker.signals.succeeded, timeout=2000):
        seen = []
        worker.signals.progress.connect(lambda d, t: seen.append((d, t)))
        pool.start(worker)

    assert seen == [(5, 10), (10, 10)]
