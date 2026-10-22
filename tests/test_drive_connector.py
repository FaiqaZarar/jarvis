"""J-11 (phase 1) acceptance checks for the Google Drive connector.

No real API calls: a stub stands in for the Drive v3 service object. The
stub mimics the exact call chains the connector uses -
``service.files().list(...).execute()``,
``service.files().export_media(...).execute()``,
``service.files().get_media(...).execute()``.
"""

import json
import re
from pathlib import Path

import pytest
from googleapiclient.errors import HttpError

from jarvis_server.internal import db
from jarvis_server.internal.migrate import run_migrations
from jarvis_server.connectors import drive
# `drive` is the package surface, used to call things and read constants.
# monkeypatch targets go on the specific submodule below instead, whenever
# the patched name is resolved as a bare name from within that submodule
# (the `drive` package-level re-export is a separate binding those calls
# never see).
from jarvis_server.connectors.drive import auth as drive_auth
from jarvis_server.connectors.drive import fetch as drive_fetch
from jarvis_server.connectors.drive import listing as drive_listing
from jarvis_server.connectors.drive import sync as drive_sync
from tests.test_filesystem_connector import _minimal_docx_bytes, _minimal_pdf_bytes

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF_MIME = "application/pdf"
GDOC_MIME = "application/vnd.google-apps.document"


# ---------------------------------------------------------------------
# Stub Drive service
# ---------------------------------------------------------------------

class _FakeRequest:
    def __init__(self, service, name, result=None, exc: Exception | None = None):
        self._s = service
        self._name = name
        self._result = result
        self._exc = exc

    def execute(self, **kwargs):
        # Record the kwargs .execute() was called with (J-11 phase 3 adds
        # num_retries here) as "<method>.execute" alongside the method-level
        # call already recorded by the _FakeFiles / _FakeChanges method.
        self._s.calls.append((self._name + ".execute", dict(kwargs)))
        if self._exc is not None:
            raise self._exc
        return self._result


class _FakeFiles:
    def __init__(self, service: "_FakeDriveService"):
        self._s = service

    def list(self, **kwargs):
        self._s.calls.append(("list", kwargs))
        if self._s.list_exc is not None:
            return _FakeRequest(self._s, "list", exc=self._s.list_exc)
        q = kwargs.get("q", "")
        m = re.search(r"'([^']+)' in parents", q)
        parent = m.group(1) if m else "root"
        # folder-only query (list_drive_folders) -> folders_by_parent;
        # every-file query (list_drive_files / _recursive) -> files_by_parent,
        # if the test set one up. Otherwise fall through to the flat `listing`.
        if "application/vnd.google-apps.folder" in q and self._s.folders_by_parent is not None:
            files = list(self._s.folders_by_parent.get(parent, []))
        elif self._s.files_by_parent is not None:
            files = list(self._s.files_by_parent.get(parent, []))
        else:
            files = list(self._s.listing)
        return _FakeRequest(self._s, "list", result={"files": files})

    def export_media(self, *, fileId, mimeType):
        self._s.calls.append(("export_media", {"fileId": fileId, "mimeType": mimeType}))
        entry = self._s.content[fileId]
        exc = entry if isinstance(entry, Exception) else None
        return _FakeRequest(self._s, "export_media", result=None if exc else entry, exc=exc)

    def get_media(self, *, fileId):
        self._s.calls.append(("get_media", {"fileId": fileId}))
        entry = self._s.content[fileId]
        exc = entry if isinstance(entry, Exception) else None
        return _FakeRequest(self._s, "get_media", result=None if exc else entry, exc=exc)

    def get(self, *, fileId, fields=None):
        self._s.calls.append(("get", {"fileId": fileId, "fields": fields}))
        entry = self._s.targets[fileId]
        exc = entry if isinstance(entry, Exception) else None
        return _FakeRequest(self._s, "get", result=None if exc else entry, exc=exc)


class _FakeChanges:
    """Stub for service.changes() - Changes API (J-11 phase 2)."""

    def __init__(self, service: "_FakeDriveService"):
        self._s = service

    def getStartPageToken(self):
        self._s.calls.append(("changes.getStartPageToken", {}))
        return _FakeRequest(
            self._s, "changes.getStartPageToken",
            result={"startPageToken": self._s.start_page_token},
        )

    def list(self, **kwargs):
        self._s.calls.append(("changes.list", kwargs))
        page = self._s.changes_pages[self._s._changes_page_idx]
        self._s._changes_page_idx += 1
        exc = page if isinstance(page, Exception) else None
        return _FakeRequest(self._s, "changes.list", result=None if exc else page, exc=exc)


class _FakeDriveService:
    def __init__(
        self,
        content: dict | None = None,
        listing: list | None = None,
        targets: dict | None = None,
        start_page_token: str = "SPT-fresh",
        changes_pages: list | None = None,
        folders_by_parent: dict | None = None,
        files_by_parent: dict | None = None,
        list_exc: Exception | None = None,
    ):
        # fileId -> bytes to return from export_media/get_media, or an
        # Exception instance to raise on .execute()
        self.content = content or {}
        self.listing = listing or []
        # fileId -> target metadata dict for files().get (shortcut
        # resolution), or an Exception instance to raise on .execute()
        self.targets = targets or {}
        self.calls: list[tuple] = []
        # parent_id -> [{"id","name"}, ...] for list_drive_folders (folder
        # picker). None -> folder queries fall through to `listing`.
        self.folders_by_parent = folders_by_parent
        # parent_id -> [file/folder dicts] for list_drive_files /
        # list_drive_files_recursive (recursive walk). Folder entries carry
        # mimeType 'application/vnd.google-apps.folder'. None -> flat `listing`.
        self.files_by_parent = files_by_parent
        # if set, files().list().execute() raises this (rate-limit tests)
        self.list_exc = list_exc
        # Changes API (J-11 phase 2): the token changes.getStartPageToken()
        # hands back, and the ordered pages changes.list() walks (each a
        # dict with optional changes/nextPageToken/newStartPageToken, or an
        # Exception instance to raise).
        self.start_page_token = start_page_token
        self.changes_pages = changes_pages or []
        self._changes_page_idx = 0

    def files(self):
        return _FakeFiles(self)

    def changes(self):
        return _FakeChanges(self)


def _http_error(status: int, reason: str = "Not Found") -> HttpError:
    resp = type("Resp", (), {"status": status, "reason": reason})()
    return HttpError(resp, f'{{"error": {{"code": {status}}}}}'.encode())


def _export_size_error() -> HttpError:
    """The 403 the Docs export raises when the result would top 10 MB."""
    resp = type("Resp", (), {"status": 403, "reason": "Forbidden"})()
    content = (
        b'{"error": {"code": 403, "errors": [{"reason": "exportSizeLimitExceeded", '
        b'"message": "This file is too large to be exported."}]}}'
    )
    return HttpError(resp, content)


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------

@pytest.fixture
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    connection = db.get_connection(db_path)
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def _staging_in_tmp(tmp_path: Path, monkeypatch):
    """Keep every staged file inside the test's tmp dir, never the repo root."""
    staging = tmp_path / "jarvis_staging"
    monkeypatch.setattr(drive_fetch, "STAGING_DIR", staging)
    return staging


NOW = "2026-09-02T00:00:00+00:00"

# sync_drive() requires a real folder_id (issue: unscoped-listing fix). The
# fake service's fake list() doesn't actually filter by it, so any non-empty
# string exercises the "a folder_id was required and supplied" path.
TEST_FOLDER_ID = "folder-under-test"


# ---------------------------------------------------------------------
# 1. Google Doc -> exported to Markdown -> normalized + stored
# ---------------------------------------------------------------------

def test_google_doc_is_exported_and_stored(conn, _staging_in_tmp):
    service = _FakeDriveService(
        content={"doc1": b"# Meeting notes\n\nShipped the search tool.\n"}
    )
    meta = {
        "id": "doc1",
        "name": "Meeting notes",
        "mimeType": GDOC_MIME,
        "modifiedTime": "2026-09-01T12:00:00.000Z",
    }

    status = drive.process_drive_file(conn, service, meta, now=NOW)

    assert status == "stored"
    # exported via export_media to text/markdown, not get_media
    assert ("export_media", {"fileId": "doc1", "mimeType": "text/markdown"}) in service.calls

    docs = conn.execute("SELECT * FROM documents").fetchall()
    assert len(docs) == 1
    assert "Shipped the search tool." in docs[0]["normalized_markdown"]

    src = conn.execute("SELECT * FROM document_sources").fetchone()
    assert src["connector"] == "drive"
    assert src["source_id"] == "doc1"
    assert src["mime"] == GDOC_MIME          # original Drive type, not text/markdown
    assert src["title"] == "Meeting notes"
    assert src["source_modified_at"] == "2026-09-01T12:00:00.000Z"


# ---------------------------------------------------------------------
# 2. Native .docx and PDF -> downloaded -> normalized + stored
# ---------------------------------------------------------------------

def test_native_docx_is_downloaded_and_stored(conn):
    service = _FakeDriveService(
        content={"dx1": _minimal_docx_bytes("Real docx pulled from Drive.")}
    )
    meta = {"id": "dx1", "name": "report.docx", "mimeType": DOCX_MIME,
            "modifiedTime": "2026-08-30T09:00:00.000Z"}

    status = drive.process_drive_file(conn, service, meta, now=NOW)

    assert status == "stored"
    assert ("get_media", {"fileId": "dx1"}) in service.calls
    doc = conn.execute("SELECT * FROM documents").fetchone()
    assert "Real docx pulled from Drive." in doc["normalized_markdown"]
    assert conn.execute("SELECT mime FROM document_sources").fetchone()["mime"] == DOCX_MIME


def test_native_pdf_is_downloaded_and_stored(conn):
    service = _FakeDriveService(
        content={"pdf1": _minimal_pdf_bytes("Real pdf pulled from Drive.")}
    )
    meta = {"id": "pdf1", "name": "spec.pdf", "mimeType": PDF_MIME,
            "modifiedTime": "2026-08-30T09:00:00.000Z"}

    status = drive.process_drive_file(conn, service, meta, now=NOW)

    assert status == "stored"
    assert ("get_media", {"fileId": "pdf1"}) in service.calls
    doc = conn.execute("SELECT * FROM documents").fetchone()
    assert "Real pdf pulled from Drive." in doc["normalized_markdown"]


def test_conversion_failure_is_reported_as_failed_not_skipped(conn, monkeypatch):
    """A converter that's actually dispatched and fails (e.g. magika's
    packaged-build MagikaError - issue #114) must surface as "failed", and
    the sync loop must count it in stats["errors"] - not stats["skipped"],
    which made real ingest failures invisible in the sync summary
    (issue #115)."""
    from jarvis_server.internal.normalizer.converters.docx import DocxNormalizer

    monkeypatch.setattr(DocxNormalizer, "convert", lambda self, file: (None, "boom"))
    meta = {"id": "dx1", "name": "report.docx", "mimeType": DOCX_MIME,
            "modifiedTime": "2026-08-30T09:00:00.000Z"}
    service = _FakeDriveService(
        content={"dx1": _minimal_docx_bytes("Never actually converted.")},
        listing=[meta],
    )

    status = drive.process_drive_file(conn, service, meta, now=NOW)
    assert status == "failed"
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0

    stats = drive.sync_drive(conn, service, folder_id=TEST_FOLDER_ID, connector="drive")
    assert stats["errors"] == 1
    assert stats["skipped"] == 0


def test_is_clean_pdf_placeholder_always_returns_true():
    """Placeholder behavior locked in - see issue #75.
    This test should be the first thing that breaks/gets removed
    when the real check is implemented."""
    assert drive._is_clean_pdf(b"anything") is True


# ---------------------------------------------------------------------
# 3. Unsupported types -> skipped, logged, never errored
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "mime, name",
    [
        ("application/vnd.google-apps.folder", "My Folder"),
        ("application/vnd.google-apps.spreadsheet", "Budget"),
        ("application/vnd.google-apps.presentation", "Deck"),
        ("image/png", "diagram.png"),
    ],
)
def test_unsupported_type_is_skipped_and_logged(conn, caplog, mime, name):
    service = _FakeDriveService(content={"x": b"should never be fetched"})
    meta = {"id": "x", "name": name, "mimeType": mime, "modifiedTime": "2026-01-01T00:00:00.000Z"}

    with caplog.at_level("INFO"):
        status = drive.process_drive_file(conn, service, meta, now=NOW)

    assert status == "skipped"
    assert service.calls == []                       # nothing fetched at all
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    assert any("out of scope" in r.message for r in caplog.records)


# ---------------------------------------------------------------------
# 4. Doc export over the 10 MB cap -> skipped with a WARNING, no crash
# ---------------------------------------------------------------------

def test_doc_over_export_cap_is_skipped_with_warning(conn, caplog):
    service = _FakeDriveService(content={"big": _export_size_error()})
    meta = {"id": "big", "name": "Huge doc", "mimeType": GDOC_MIME,
            "modifiedTime": "2026-01-01T00:00:00.000Z"}

    with caplog.at_level("WARNING"):
        status = drive.process_drive_file(conn, service, meta, now=NOW)

    assert status == "skipped"
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("10 MB" in r.message and "Huge doc" in r.message for r in warnings)


def test_non_size_http_error_still_propagates(conn):
    """A 500 (or any non-size 4xx) is a real failure - it must NOT be
    silently swallowed as a skip."""
    resp = type("Resp", (), {"status": 500, "reason": "Backend Error"})()
    service = _FakeDriveService(content={"d": HttpError(resp, b'{"error": {"code": 500}}')})
    meta = {"id": "d", "name": "doc", "mimeType": GDOC_MIME, "modifiedTime": "2026-01-01T00:00:00.000Z"}

    with pytest.raises(HttpError):
        drive.process_drive_file(conn, service, meta, now=NOW)


# ---------------------------------------------------------------------
# Staging dir defaults to JARVIS_DAEMON_HOME, not a repo-relative path
# (real bug: previously always parents[5], meaningless once frozen -
# would resolve into PyInstaller's temp extraction dir, or raise
# IndexError at import time and take down daemon startup).
# ---------------------------------------------------------------------

def test_default_staging_dir_uses_daemon_home_when_set(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DAEMON_HOME", str(tmp_path))
    assert drive_fetch._default_staging_dir() == tmp_path / "jarvis_staging"


def test_default_staging_dir_falls_back_to_repo_root_when_unset(monkeypatch):
    monkeypatch.delenv("JARVIS_DAEMON_HOME", raising=False)
    assert drive_fetch._default_staging_dir().name == "jarvis_staging"
    assert drive_fetch._default_staging_dir() == drive_fetch._default_staging_dir()  # stable


# ---------------------------------------------------------------------
# 5. Staged file: deleted on success, left in place on failure
# ---------------------------------------------------------------------

def test_staged_file_is_deleted_on_success(conn, _staging_in_tmp):
    service = _FakeDriveService(content={"doc1": b"# Notes\n\nbody\n"})
    meta = {"id": "doc1", "name": "Notes", "mimeType": GDOC_MIME, "modifiedTime": "2026-01-01T00:00:00.000Z"}

    drive.process_drive_file(conn, service, meta, now=NOW)

    assert list(_staging_in_tmp.iterdir()) == []          # nothing left behind


def test_staged_file_is_left_in_place_on_failure(conn, _staging_in_tmp, monkeypatch):
    service = _FakeDriveService(content={"doc1": b"# Notes\n\nbody\n"})
    meta = {"id": "doc1", "name": "Notes", "mimeType": GDOC_MIME, "modifiedTime": "2026-01-01T00:00:00.000Z"}

    def boom(*a, **k):
        raise RuntimeError("simulated DB failure at commit")

    monkeypatch.setattr(drive._common, "store_document", boom)

    with pytest.raises(RuntimeError):
        drive.process_drive_file(conn, service, meta, now=NOW)

    staged = _staging_in_tmp / "doc1.md"
    assert staged.exists()                                # retryable on the next run
    assert staged.read_bytes() == b"# Notes\n\nbody\n"


# ---------------------------------------------------------------------
# 6. Identical content across two Drive files -> one documents row
# ---------------------------------------------------------------------

def test_identical_content_dedups_to_one_document(conn):
    body = b"# Shared\n\nsame bytes in two Drive files\n"
    service = _FakeDriveService(content={"a": body, "b": body})

    for fid in ("a", "b"):
        meta = {"id": fid, "name": f"{fid}.doc", "mimeType": GDOC_MIME,
                "modifiedTime": "2026-01-01T00:00:00.000Z"}
        assert drive.process_drive_file(conn, service, meta, now=NOW) == "stored"

    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM document_sources").fetchone()[0] == 2
    hashes = {r["content_hash"] for r in conn.execute("SELECT content_hash FROM document_sources")}
    assert len(hashes) == 1                               # both point at the one document


# ---------------------------------------------------------------------
# 7. list_drive_files: paginates, stays off Shared Drives
# ---------------------------------------------------------------------

def test_list_drive_files_does_not_touch_shared_drives(conn):
    service = _FakeDriveService(
        listing=[
            {"id": "1", "name": "a.doc", "mimeType": GDOC_MIME, "modifiedTime": "t"},
            {"id": "2", "name": "b.pdf", "mimeType": PDF_MIME, "modifiedTime": "t"},
        ]
    )

    files = drive.list_drive_files(service)

    assert [f["id"] for f in files] == ["1", "2"]
    (_, kwargs) = next(c for c in service.calls if c[0] == "list")
    # Shared Drives are out of MVP scope - these flags must never be set.
    assert "supportsAllDrives" not in kwargs
    assert "includeItemsFromAllDrives" not in kwargs
    assert "trashed = false" in kwargs["q"]


def test_list_drive_files_scopes_to_a_folder_when_asked(conn):
    service = _FakeDriveService(listing=[])
    drive.list_drive_files(service, folder_id="FOLDER123")
    (_, kwargs) = next(c for c in service.calls if c[0] == "list")
    assert "'FOLDER123' in parents" in kwargs["q"]


def test_list_drive_files_requests_shortcut_details_and_drive_id(conn):
    service = _FakeDriveService(listing=[])
    drive.list_drive_files(service)
    (_, kwargs) = next(c for c in service.calls if c[0] == "list")
    assert "shortcutDetails" in kwargs["fields"]
    # driveId must be requested too, or the shared-drive flag can never be
    # set on the full-sync path (adversarial review 1b).
    assert "driveId" in kwargs["fields"]


# ---------------------------------------------------------------------
# 7b. list_drive_folders: powers the folder picker (python -m jarvis.cmd.drive_ingest)
# ---------------------------------------------------------------------

def test_list_drive_folders_queries_one_parent_for_folders_only(conn):
    service = _FakeDriveService(folders_by_parent={
        "root": [{"id": "f1", "name": "Documents"}, {"id": "f2", "name": "Photos"}],
    })

    folders = drive.list_drive_folders(service)  # default parent = "root"

    assert folders == [{"id": "f1", "name": "Documents"}, {"id": "f2", "name": "Photos"}]
    (_, kwargs) = next(c for c in service.calls if c[0] == "list")
    assert "'root' in parents" in kwargs["q"]
    assert "mimeType = 'application/vnd.google-apps.folder'" in kwargs["q"]
    assert "trashed = false" in kwargs["q"]


def test_list_drive_folders_scopes_to_the_given_parent(conn):
    service = _FakeDriveService(folders_by_parent={
        "root": [{"id": "f1", "name": "Documents"}],
        "f1": [{"id": "f1a", "name": "Invoices"}],
    })

    subs = drive.list_drive_folders(service, parent_id="f1")

    assert subs == [{"id": "f1a", "name": "Invoices"}]
    (_, kwargs) = next(c for c in service.calls if c[0] == "list")
    assert "'f1' in parents" in kwargs["q"]


def test_list_drive_folders_empty_when_a_folder_has_no_subfolders(conn):
    service = _FakeDriveService(folders_by_parent={"root": []})
    assert drive.list_drive_folders(service) == []


# ---------------------------------------------------------------------
# 7b2. list_drive_files_recursive: walk the whole sub-tree, like os.walk
# ---------------------------------------------------------------------

_FOLDER = "application/vnd.google-apps.folder"


def _folder_entry(fid, name="f"):
    return {"id": fid, "name": name, "mimeType": _FOLDER}


def _file_entry(fid, mime=GDOC_MIME, name=None):
    return {"id": fid, "name": name or fid, "mimeType": mime, "modifiedTime": "t"}


def test_list_drive_files_recursive_reaches_files_at_every_depth(conn):
    service = _FakeDriveService(files_by_parent={
        "top": [_file_entry("a"), _folder_entry("sub1"), _folder_entry("sub2")],
        "sub1": [_file_entry("b"), _folder_entry("sub1a")],
        "sub1a": [_file_entry("c")],                       # depth 3
        "sub2": [_file_entry("d")],
    })

    found = drive.list_drive_files_recursive(service, "top")

    assert {f["id"] for f in found} == {"a", "b", "c", "d"}   # every depth, no folders
    assert all(f["mimeType"] != _FOLDER for f in found)


def test_list_drive_files_recursive_dedups_a_file_with_two_parents(conn):
    # 'shared' lives in both sub1 and sub2 (Drive multi-parent)
    service = _FakeDriveService(files_by_parent={
        "top": [_folder_entry("sub1"), _folder_entry("sub2")],
        "sub1": [_file_entry("shared"), _file_entry("only1")],
        "sub2": [_file_entry("shared"), _file_entry("only2")],
    })

    found = drive.list_drive_files_recursive(service, "top")

    ids = [f["id"] for f in found]
    assert sorted(ids) == ["only1", "only2", "shared"]        # 'shared' once, not twice


def test_list_drive_files_recursive_cycle_guard_prevents_infinite_recursion(conn):
    # sub -> back to top -> sub -> ... would loop forever without `visited`
    service = _FakeDriveService(files_by_parent={
        "top": [_file_entry("t"), _folder_entry("sub")],
        "sub": [_file_entry("s"), _folder_entry("top")],     # points back at an ancestor
    })

    found = drive.list_drive_files_recursive(service, "top")

    assert {f["id"] for f in found} == {"t", "s"}
    # each folder listed exactly once despite the loop
    list_parents = [
        re.search(r"'([^']+)' in parents", kw["q"]).group(1)
        for name, kw in service.calls if name == "list"
    ]
    assert sorted(list_parents) == ["sub", "top"]


def test_sync_drive_full_recursively_ingests_nested_files_and_resolves_a_nested_shortcut(conn):
    """The recursive walk must (a) reach a file two folders deep and
    (b) still hand a shortcut it finds down there to
    resolve_shortcut_target (Phase 1 logic, invoked via
    process_drive_file)."""
    target = {"id": "tgt", "name": "Target Doc", "mimeType": GDOC_MIME,
              "modifiedTime": "2026-01-01T00:00:00.000Z"}
    nested_shortcut = {"id": "sc1", "name": "SC", "mimeType": _SHORTCUT,
                       "modifiedTime": "t", "shortcutDetails": {"targetId": "tgt"}}
    service = _FakeDriveService(
        content={"deep": b"# Deep\n\nbody\n", "tgt": b"# Target\n\nbody\n"},
        targets={"tgt": target},
        files_by_parent={
            "root_folder": [_folder_entry("level2")],
            "level2": [_file_entry("deep"), nested_shortcut],
        },
    )

    stats = drive._sync_drive_full(conn, service, folder_id="root_folder", connector="drive")

    assert stats["pulled"] == 2 and stats["errors"] == 0       # deep doc + resolved shortcut
    assert ("get", {"fileId": "tgt", "fields": _TARGET_FIELDS}) in service.calls  # resolution fired
    assert {r["source_id"] for r in conn.execute("SELECT source_id FROM document_sources")} == {"deep", "sc1"}


# ---------------------------------------------------------------------
# 7c. is_rate_limited + the wrapped listing call in _sync_drive_full
# ---------------------------------------------------------------------

def test_is_rate_limited_recognises_429_and_403_quota_reasons(conn):
    assert drive.is_rate_limited(_http_error(429, "Too Many Requests")) is True

    resp = type("R", (), {"status": 403, "reason": "Forbidden"})()
    quota_403 = HttpError(
        resp, b'{"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}'
    )
    assert drive.is_rate_limited(quota_403) is True

    # a plain 403 (permission) is NOT a rate limit
    assert drive.is_rate_limited(_http_error(403, "Forbidden")) is False
    assert drive.is_rate_limited(_http_error(404, "Not Found")) is False


def test_sync_drive_full_rate_limited_listing_gives_a_clear_message_not_a_traceback(conn):
    """Rank 3 finding: an uncaught HttpError on the folder listing used to
    crash the whole ingest with a raw googleapiclient traceback. A
    rate-limit on that one call must surface as a clear, actionable
    RuntimeError - nothing was ingested, so re-running later is safe."""
    service = _FakeDriveService(list_exc=_http_error(429, "Too Many Requests"))

    with pytest.raises(RuntimeError, match="rate-limiting"):
        drive._sync_drive_full(conn, service, folder_id="F", connector="drive")

    # a non-rate-limit HttpError still propagates as-is (unchanged behavior)
    service2 = _FakeDriveService(list_exc=_http_error(500, "Internal Error"))
    with pytest.raises(HttpError):
        drive._sync_drive_full(conn, service2, folder_id="F", connector="drive")


# ---------------------------------------------------------------------
# 8. Shortcut resolution (docs/J11_DESIGN.md §3)
# ---------------------------------------------------------------------

_SHORTCUT = "application/vnd.google-apps.shortcut"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
_TARGET_FIELDS = "id, name, mimeType, modifiedTime"


def test_shortcut_to_google_doc_resolves_and_keeps_shortcut_identity(conn):
    """§3.1/§3.3: source_id + source_url follow the shortcut, but content,
    mime, and title come from the live-resolved target."""
    service = _FakeDriveService(
        content={"tgt1": b"# Real target\n\nActual doc body from the target.\n"},
        targets={"tgt1": {
            "id": "tgt1", "name": "Real Target Doc",
            "mimeType": GDOC_MIME, "modifiedTime": "2026-08-01T00:00:00.000Z",
        }},
    )
    meta = {
        "id": "sc1", "name": "Shortcut to Real Target Doc",
        "mimeType": _SHORTCUT, "modifiedTime": "2026-09-01T00:00:00.000Z",
        "shortcutDetails": {"targetId": "tgt1", "targetMimeType": GDOC_MIME},
    }

    status = drive.process_drive_file(conn, service, meta, now=NOW)

    assert status == "stored"
    # resolved live via files.get (never trusting targetMimeType), then
    # exported the TARGET's id
    assert ("get", {"fileId": "tgt1", "fields": _TARGET_FIELDS}) in service.calls
    assert ("export_media", {"fileId": "tgt1", "mimeType": "text/markdown"}) in service.calls

    src = conn.execute("SELECT * FROM document_sources").fetchone()
    assert src["source_id"] == "sc1"                       # identity = the shortcut
    assert src["source_url"] == "https://drive.google.com/open?id=sc1"
    assert src["mime"] == GDOC_MIME                        # target's real type, not _SHORTCUT
    assert src["title"] == "Real Target Doc"               # target's name
    doc = conn.execute("SELECT * FROM documents").fetchone()
    assert "Actual doc body from the target." in doc["normalized_markdown"]


def test_shortcut_stores_targets_modified_time_not_the_shortcuts(conn):
    """§3.3: source_modified_at is content-recency, so it follows the
    resolved target - the shortcut's own modifiedTime is placement
    metadata, not content."""
    service = _FakeDriveService(
        content={"tgt2": b"# Target\n\nbody\n"},
        targets={"tgt2": {
            "id": "tgt2", "name": "Target Doc",
            "mimeType": GDOC_MIME, "modifiedTime": "2026-03-15T08:00:00.000Z",
        }},
    )
    meta = {
        "id": "sc7", "name": "Shortcut made much later",
        "mimeType": _SHORTCUT, "modifiedTime": "2026-09-02T23:59:00.000Z",
        "shortcutDetails": {"targetId": "tgt2"},
    }

    assert drive.process_drive_file(conn, service, meta, now=NOW) == "stored"

    src = conn.execute("SELECT * FROM document_sources").fetchone()
    assert src["source_modified_at"] == "2026-03-15T08:00:00.000Z"   # target's
    assert src["source_modified_at"] != "2026-09-02T23:59:00.000Z"   # not the shortcut's


def test_shortcut_to_unsupported_type_skips_cleanly(conn, caplog):
    service = _FakeDriveService(
        targets={"sheet1": {
            "id": "sheet1", "name": "Budget Sheet",
            "mimeType": SHEET_MIME, "modifiedTime": "t",
        }},
    )
    meta = {"id": "sc2", "name": "Shortcut to Budget", "mimeType": _SHORTCUT,
            "modifiedTime": "t", "shortcutDetails": {"targetId": "sheet1"}}

    with caplog.at_level("INFO"):
        status = drive.process_drive_file(conn, service, meta, now=NOW)

    assert status == "skipped"
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    # it WAS resolved; the target's real type then hit the out-of-scope branch
    assert ("get", {"fileId": "sheet1", "fields": _TARGET_FIELDS}) in service.calls
    assert any("out of scope" in r.message for r in caplog.records)


def test_broken_shortcut_missing_target_skips_with_warning(conn, caplog):
    service = _FakeDriveService()
    meta = {"id": "sc3", "name": "Dangling shortcut", "mimeType": _SHORTCUT,
            "modifiedTime": "t", "shortcutDetails": {}}

    with caplog.at_level("WARNING"):
        status = drive.process_drive_file(conn, service, meta, now=NOW)

    assert status == "skipped"
    assert service.calls == []                             # files.get never attempted
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any(
        "broken shortcut" in r.message and "Dangling shortcut" in r.message
        for r in warnings
    )


def test_shortcut_with_vanished_target_skips_with_warning(conn, caplog):
    service = _FakeDriveService(targets={"gone": _http_error(404)})
    meta = {"id": "sc4", "name": "Shortcut to a deleted file", "mimeType": _SHORTCUT,
            "modifiedTime": "t", "shortcutDetails": {"targetId": "gone"}}

    with caplog.at_level("WARNING"):
        status = drive.process_drive_file(conn, service, meta, now=NOW)

    assert status == "skipped"
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any("vanished or inaccessible" in m for m in warnings)
    assert not any("resource key" in m for m in warnings)


def test_shortcut_target_needing_resource_key_gets_distinct_warning(conn, caplog):
    """§3.4: a 403/404 WITH a targetResourceKey is a known, specific gotcha -
    it must not be misread as a plain vanished target. (Unchanged by the
    403/404 split - the resource-key check runs first, for either status.)"""
    service = _FakeDriveService(targets={"gated": _http_error(403, "Forbidden")})
    meta = {"id": "sc5", "name": "Shortcut to a gated file", "mimeType": _SHORTCUT,
            "modifiedTime": "t",
            "shortcutDetails": {"targetId": "gated", "targetResourceKey": "rk-abc123"}}

    with caplog.at_level("WARNING"):
        status = drive.process_drive_file(conn, service, meta, now=NOW)

    assert status == "skipped"
    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any("requires a resource key" in m for m in warnings)
    assert not any("vanished or inaccessible" in m for m in warnings)


def test_shortcut_target_bare_403_re_raises_rather_than_silently_skipping(conn):
    """Adversarial review 1a/5: a bare 403 (no resource key) is ambiguous -
    it can be a persisted rate-limit / dailyLimitExceeded / permission
    change on a file that still exists, NOT proof the target is gone.
    resolve_shortcut_target must re-raise it (so process_drive_file's
    `except Exception` counts it as a real error) rather than return None /
    "skipped". Only a genuine 404 is treated as "vanished forever"."""
    service = _FakeDriveService(targets={"maybe-rate-limited": _http_error(403, "Forbidden")})
    meta = {"id": "sc7", "name": "Shortcut to a maybe-rate-limited file",
            "mimeType": _SHORTCUT, "modifiedTime": "t",
            "shortcutDetails": {"targetId": "maybe-rate-limited"}}   # NO targetResourceKey

    with pytest.raises(HttpError):
        drive.process_drive_file(conn, service, meta, now=NOW)

    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def test_shortcut_to_a_shortcut_is_not_recursed(conn, caplog):
    service = _FakeDriveService(
        targets={"sc_inner": {
            "id": "sc_inner", "name": "Another shortcut",
            "mimeType": _SHORTCUT, "modifiedTime": "t",
        }},
    )
    meta = {"id": "sc6", "name": "Shortcut to a shortcut", "mimeType": _SHORTCUT,
            "modifiedTime": "t", "shortcutDetails": {"targetId": "sc_inner"}}

    with caplog.at_level("WARNING"):
        status = drive.process_drive_file(conn, service, meta, now=NOW)

    assert status == "skipped"
    # exactly one files.get - the inner shortcut is NOT itself resolved
    assert [c for c in service.calls if c[0] == "get"] == [
        ("get", {"fileId": "sc_inner", "fields": _TARGET_FIELDS})
    ]
    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any("another shortcut" in m for m in warnings)


def test_two_shortcuts_to_same_target_dedup_to_one_document(conn):
    body = b"# Shared target\n\none body, reached via two different shortcuts\n"
    service = _FakeDriveService(
        content={"tgtX": body},
        targets={"tgtX": {
            "id": "tgtX", "name": "Shared Target",
            "mimeType": GDOC_MIME, "modifiedTime": "t",
        }},
    )

    for sc in ("scA", "scB"):
        meta = {"id": sc, "name": f"Shortcut {sc}", "mimeType": _SHORTCUT,
                "modifiedTime": "t", "shortcutDetails": {"targetId": "tgtX"}}
        assert drive.process_drive_file(conn, service, meta, now=NOW) == "stored"

    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    rows = conn.execute(
        "SELECT source_id FROM document_sources ORDER BY source_id"
    ).fetchall()
    assert [r["source_id"] for r in rows] == ["scA", "scB"]
    hashes = {r["content_hash"] for r in conn.execute("SELECT content_hash FROM document_sources")}
    assert len(hashes) == 1


# ---------------------------------------------------------------------
# 9. Shared-drive flag -> native_metadata_json (docs/J11_DESIGN.md §3.5)
# ---------------------------------------------------------------------

def test_shared_drive_item_records_native_metadata_flag(conn):
    service = _FakeDriveService(content={"sd1": b"# From a shared drive\n\nbody\n"})
    meta = {"id": "sd1", "name": "Shared-drive doc", "mimeType": GDOC_MIME,
            "modifiedTime": "t", "driveId": "0AXYZ-shared-drive-id"}

    assert drive.process_drive_file(conn, service, meta, now=NOW) == "stored"

    raw = conn.execute("SELECT native_metadata_json FROM document_sources").fetchone()[0]
    assert json.loads(raw) == {"shared_drive": True}


def test_my_drive_item_leaves_native_metadata_null(conn):
    service = _FakeDriveService(content={"d1": b"# Plain My Drive doc\n\nbody\n"})
    meta = {"id": "d1", "name": "My Drive doc", "mimeType": GDOC_MIME, "modifiedTime": "t"}

    assert drive.process_drive_file(conn, service, meta, now=NOW) == "stored"

    assert conn.execute(
        "SELECT native_metadata_json FROM document_sources"
    ).fetchone()[0] is None


def test_shared_drive_flag_wired_end_to_end_through_the_full_sync_loop(conn):
    """The full-sync loop - list_drive_files -> process_drive_file ->
    _common.ingest_staged_file -> store_document -> insert_document_source -
    must carry ``driveId`` from the listing entry all the way to
    ``native_metadata_json`` on the row, not just have the two halves work
    in isolation. Regression for adversarial review finding 1b: ``driveId``
    was missing from ``list_drive_files``'s fields, so on this path the flag
    was never set even though ``process_drive_file`` handled it correctly."""
    shared = _gdoc("shared1", "2026-01-01T00:00:00.000Z", "Shared-with-me doc")
    shared["driveId"] = "0AXYZ-shared-drive-id"          # as list_drive_files now returns it
    plain = _gdoc("plain1", "2026-01-01T00:00:00.000Z", "My Drive doc")   # no driveId
    service = _FakeDriveService(
        content={"shared1": b"# Shared\n\nbody\n", "plain1": b"# Plain\n\nbody\n"},
        listing=[shared, plain],
    )

    stats = drive.sync_drive(conn, service, folder_id=TEST_FOLDER_ID, connector="drive")

    assert stats["pulled"] == 2 and stats["errors"] == 0

    rows = {
        r["source_id"]: r["native_metadata_json"]
        for r in conn.execute(
            "SELECT source_id, native_metadata_json FROM document_sources"
        )
    }
    assert json.loads(rows["shared1"]) == {"shared_drive": True}   # wired through the loop
    assert rows["plain1"] is None                                  # not set for a My Drive file


# ---------------------------------------------------------------------
# 10. _sync_drive_full: Phase A fast-skip (docs/J11_PHASE4_DESIGN.md
#     §3.1-§3.4, §5) + required folder_id (issue: unscoped-listing fix).
#     Phase B (tombstoning) is currently disabled - see section 10b and
#     issue #78.
#
# These exercise the full-listing path directly. Since J-11 phase 2,
# public sync_drive() dispatches to the incremental path once a
# start_page_token is stored, so a test that needs the full path on a
# *second* call must call _sync_drive_full explicitly. The hybrid
# dispatch itself is covered in section 11.
# ---------------------------------------------------------------------

import datetime as _dt  # noqa: E402


class _FrozenClock:
    """Stand-in for the `datetime` name inside drive.py, so sync_drive's
    `datetime.now(timezone.utc).isoformat()` returns a controlled value."""

    def __init__(self, value: _dt.datetime):
        self._value = value

    def now(self, tz=None):
        return self._value


def _gdoc(fid: str, modified: str, name: str | None = None) -> dict:
    return {"id": fid, "name": name or fid, "mimeType": GDOC_MIME, "modifiedTime": modified}


# ---------------------------------------------------------------------
# 10a. folder_id is required - no whole-Drive mode (unscoped-listing fix,
#      confirmed real via live test: 1,567 files / 910 downloadable, zero
#      scoping)
# ---------------------------------------------------------------------

def test_sync_drive_without_folder_id_raises_clearly(conn):
    service = _FakeDriveService(listing=[])

    with pytest.raises(ValueError, match="folder_id"):
        drive.sync_drive(conn, service, folder_id=None, connector="drive")

    with pytest.raises(ValueError, match="folder_id"):
        drive.sync_drive(conn, service, folder_id="", connector="drive")

    assert service.calls == []   # never even reached files().list()


def test_sync_drive_full_without_folder_id_raises_clearly(conn):
    """_sync_drive_full is called directly throughout this file (see the
    section 10 header) once a cursor exists, so it needs the same guard
    as the public sync_drive() entrypoint, not just its caller's."""
    service = _FakeDriveService(listing=[])

    with pytest.raises(ValueError, match="folder_id"):
        drive._sync_drive_full(conn, service, folder_id=None, connector="drive")

    assert service.calls == []


def test_sync_drive_threads_folder_id_into_the_real_list_call(conn):
    """Closes the exact gap found during live testing: list_drive_files
    already supported folder_id, but nothing on the sync/ingest path ever
    passed one. Confirms sync_drive's folder_id genuinely reaches the
    files().list() query, not just that the parameter exists."""
    service = _FakeDriveService(listing=[])

    drive.sync_drive(conn, service, folder_id="real-folder-abc123", connector="drive")

    list_calls = [kwargs for name, kwargs in service.calls if name == "list"]
    assert len(list_calls) == 1
    assert "'real-folder-abc123' in parents" in list_calls[0]["q"]


def test_sync_drive_fast_skips_a_file_whose_modified_time_is_unchanged(conn, monkeypatch):
    """§3.2: exact modifiedTime match -> touch_document_source_seen, no fetch."""
    doc = _gdoc("d1", "2026-08-01T00:00:00.000Z", "Notes")
    service = _FakeDriveService(content={"d1": b"# Notes\n\nbody\n"}, listing=[doc])

    monkeypatch.setattr(drive_sync, "datetime", _FrozenClock(_dt.datetime(2026, 9, 1, tzinfo=_dt.timezone.utc)))
    first = drive._sync_drive_full(conn, service, folder_id=TEST_FOLDER_ID, connector="drive")
    assert first == {"pulled": 1, "fast_skipped": 0, "skipped": 0, "tombstoned": 0, "errors": 0}
    before = conn.execute(
        "SELECT source_modified_at, source_last_seen FROM document_sources WHERE source_id = 'd1'"
    ).fetchone()
    assert before["source_modified_at"] == "2026-08-01T00:00:00.000Z"
    assert before["source_last_seen"] == "2026-09-01T00:00:00+00:00"

    # second run, identical listing: fetch_file_bytes must NOT be reached.
    def _no_fetch(*a, **k):
        raise AssertionError("fetch_file_bytes called for a fast-skippable file")
    monkeypatch.setattr(drive_fetch, "fetch_file_bytes", _no_fetch)
    monkeypatch.setattr(drive_sync, "datetime", _FrozenClock(_dt.datetime(2026, 9, 2, tzinfo=_dt.timezone.utc)))

    second = drive._sync_drive_full(conn, service, folder_id=TEST_FOLDER_ID, connector="drive")
    assert second == {"pulled": 1, "fast_skipped": 1, "skipped": 0, "tombstoned": 0, "errors": 0}
    after = conn.execute(
        "SELECT source_last_seen FROM document_sources WHERE source_id = 'd1'"
    ).fetchone()
    assert after["source_last_seen"] == "2026-09-02T00:00:00+00:00"   # touch_document_source_seen ran


def test_sync_drive_reprocesses_a_file_whose_modified_time_changed(conn):
    """§3.2: a modifiedTime mismatch means real work, not a skip."""
    service = _FakeDriveService(
        content={"d1": b"# Notes\n\nversion one\n"},
        listing=[_gdoc("d1", "2026-08-01T00:00:00.000Z")],
    )
    drive._sync_drive_full(conn, service, folder_id=TEST_FOLDER_ID, connector="drive")

    service.content["d1"] = b"# Notes\n\nversion two, edited\n"
    service.listing = [_gdoc("d1", "2026-08-09T00:00:00.000Z")]
    service.calls.clear()

    second = drive._sync_drive_full(conn, service, folder_id=TEST_FOLDER_ID, connector="drive")

    assert second["fast_skipped"] == 0
    assert second["pulled"] == 1
    assert ("export_media", {"fileId": "d1", "mimeType": "text/markdown"}) in service.calls
    src = conn.execute(
        "SELECT source_modified_at FROM document_sources WHERE source_id = 'd1'"
    ).fetchone()
    assert src["source_modified_at"] == "2026-08-09T00:00:00.000Z"
    assert "version two, edited" in conn.execute(
        "SELECT normalized_markdown FROM documents WHERE content_hash = "
        "(SELECT content_hash FROM document_sources WHERE source_id = 'd1')"
    ).fetchone()["normalized_markdown"]


def test_sync_drive_processes_a_first_time_seen_file(conn):
    """§3.2: never-seen source_id is processed regardless of modifiedTime
    (the `source_id in stored` guard stops a coincidental None==None skip)."""
    service = _FakeDriveService(
        content={"new1": b"# Fresh\n\nbody\n"},
        listing=[_gdoc("new1", "2026-08-01T00:00:00.000Z")],
    )

    stats = drive.sync_drive(conn, service, folder_id=TEST_FOLDER_ID, connector="drive")

    assert stats["fast_skipped"] == 0
    assert stats["pulled"] == 1
    assert ("export_media", {"fileId": "new1", "mimeType": "text/markdown"}) in service.calls
    assert conn.execute(
        "SELECT COUNT(*) FROM document_sources WHERE source_id = 'new1'"
    ).fetchone()[0] == 1


def test_sync_drive_never_fast_skips_a_shortcut(conn):
    """§3.2: shortcuts are excluded from the fast-skip even when the
    shortcut's OWN modifiedTime equals the stored (target's) value."""
    target = {"id": "t1", "name": "Target", "mimeType": GDOC_MIME,
              "modifiedTime": "2026-01-01T00:00:00.000Z"}
    # shortcut's own modifiedTime deliberately == the target's, so without
    # the `not is_shortcut` guard this would wrongly fast-skip.
    sc = {"id": "sc1", "name": "SC", "mimeType": _SHORTCUT,
          "modifiedTime": "2026-01-01T00:00:00.000Z",
          "shortcutDetails": {"targetId": "t1"}}
    service = _FakeDriveService(
        content={"t1": b"# Target\n\nbody\n"}, listing=[sc], targets={"t1": target}
    )

    drive._sync_drive_full(conn, service, folder_id=TEST_FOLDER_ID, connector="drive")
    stored = conn.execute(
        "SELECT source_modified_at FROM document_sources WHERE source_id = 'sc1'"
    ).fetchone()
    assert stored["source_modified_at"] == "2026-01-01T00:00:00.000Z"   # target's, per J11_DESIGN §3.3

    service.calls.clear()
    second = drive._sync_drive_full(conn, service, folder_id=TEST_FOLDER_ID, connector="drive")

    assert second["fast_skipped"] == 0
    assert ("get", {"fileId": "t1", "fields": _TARGET_FIELDS}) in service.calls
    assert ("export_media", {"fileId": "t1", "mimeType": "text/markdown"}) in service.calls


# ---------------------------------------------------------------------
# 10b. Tombstoning is disabled while folder-scoped (issue #78). These
#      replace the old "sweep" tests (§3.1, unscoped) - a vanished file
#      must now stay live, not get tombstoned, in every case a whole-Drive
#      listing used to catch.
# ---------------------------------------------------------------------

def test_sync_drive_does_not_tombstone_a_vanished_file_while_the_sweep_is_disabled(conn):
    """issue #78: with the listing folder-scoped, document_sources has no
    record of which folder a file came from, so there is no safe way to
    tell 'vanished from this folder' apart from 'just not in this folder'.
    The sweep is disabled entirely rather than risk repeating issue #61
    for Drive - a file absent from this pull must stay exactly as it was,
    not get tombstoned."""
    service = _FakeDriveService(
        content={"a": b"# A\n\naaa\n", "b": b"# B\n\nbbb\n"},
        listing=[_gdoc("a", "2026-08-01T00:00:00.000Z"), _gdoc("b", "2026-08-01T00:00:00.000Z")],
    )
    drive._sync_drive_full(conn, service, folder_id=TEST_FOLDER_ID, connector="drive")

    service.listing = [_gdoc("a", "2026-08-01T00:00:00.000Z")]      # b is gone from this listing
    second = drive._sync_drive_full(conn, service, folder_id=TEST_FOLDER_ID, connector="drive")

    assert second["tombstoned"] == 0
    assert conn.execute(
        "SELECT tombstoned FROM document_sources WHERE source_id = 'a'"
    ).fetchone()["tombstoned"] == 0
    assert conn.execute(
        "SELECT tombstoned FROM document_sources WHERE source_id = 'b'"
    ).fetchone()["tombstoned"] == 0   # NOT tombstoned - sweep is disabled (issue #78)


def test_sync_drive_tombstoned_stays_zero_even_when_a_pull_errored(conn, monkeypatch):
    """The sweep being unconditionally disabled (issue #78) subsumes the
    old spec §5.3 concern (a partial pull must never read as mass
    deletion) - there's no sweep left to protect from a partial pull
    either way, but confirming the error path doesn't change that is
    still worth locking in."""
    service = _FakeDriveService(
        content={"a": b"# A\n\naaa\n", "b": b"# B\n\nbbb\n"},
        listing=[_gdoc("a", "2026-01-01T00:00:00.000Z"), _gdoc("b", "2026-01-01T00:00:00.000Z")],
    )
    drive._sync_drive_full(conn, service, folder_id=TEST_FOLDER_ID, connector="drive")

    # b vanishes; a comes back CHANGED (so it is re-processed, not fast-skipped)
    # and that processing fails.
    service.listing = [_gdoc("a", "2026-06-01T00:00:00.000Z")]

    def _boom(*a, **k):
        raise RuntimeError("simulated transport failure during a re-fetch")
    monkeypatch.setattr(drive_sync, "process_drive_file", _boom)

    stats = drive._sync_drive_full(conn, service, folder_id=TEST_FOLDER_ID, connector="drive")

    assert stats["errors"] == 1
    assert stats["tombstoned"] == 0
    assert conn.execute(
        "SELECT tombstoned FROM document_sources WHERE source_id = 'b'"
    ).fetchone()["tombstoned"] == 0


# ---------------------------------------------------------------------
# 11. sync_drive hybrid dispatch: full vs. Changes API incremental
#     (docs/J11_PHASE2_DESIGN.md §3.2, §3.3, §3.4, §5)
# ---------------------------------------------------------------------

def test_sync_drive_no_stored_token_runs_full_path_and_persists_a_fresh_cursor(conn):
    """§3.2: first run - no cursor - takes the full path, then mints and
    stores one via changes.getStartPageToken()."""
    service = _FakeDriveService(
        content={"g1": b"# G1\n\nbody\n"},
        listing=[_gdoc("g1", "2026-08-01T00:00:00.000Z")],
        start_page_token="SPT-1",
    )

    stats = drive.sync_drive(conn, service, folder_id=TEST_FOLDER_ID, connector="drive")

    assert stats["pulled"] == 1
    assert any(c[0] == "list" for c in service.calls)                 # full listing ran
    assert ("changes.getStartPageToken", {}) in service.calls
    assert db.get_connector_state(conn, "drive", "start_page_token") == "SPT-1"


def test_sync_drive_with_stored_token_runs_incremental_and_never_lists_files(conn, monkeypatch):
    """§3.3: a stored cursor -> the incremental path; list_drive_files is
    never called (mirrors how phase 4 proved fast-skip never fetches)."""
    db.set_connector_state(conn, "drive", "start_page_token", "SPT-old", NOW)

    def _no_list(*a, **k):
        raise AssertionError("list_drive_files called on the incremental path")
    monkeypatch.setattr(drive_listing, "list_drive_files", _no_list)

    service = _FakeDriveService(
        content={"c1": b"# C1\n\nbody\n"},
        changes_pages=[{
            "changes": [{"fileId": "c1", "file": _gdoc("c1", "2026-09-01T00:00:00.000Z")}],
            "newStartPageToken": "SPT-new",
        }],
    )

    stats = drive.sync_drive(conn, service, folder_id=TEST_FOLDER_ID, connector="drive")

    assert stats["pulled"] == 1
    assert any(c[0] == "changes.list" for c in service.calls)
    assert not any(c[0] == "list" for c in service.calls)
    assert db.get_connector_state(conn, "drive", "start_page_token") == "SPT-new"


def test_sync_drive_incremental_removed_change_tombstones_directly_no_sweep(conn, monkeypatch):
    """§3.3: `removed: true` -> tombstone that fileId directly, and the
    sweep is never invoked on this path."""
    seed = _FakeDriveService(
        content={"x1": b"# X1\n\nbody\n"},
        listing=[_gdoc("x1", "2026-08-01T00:00:00.000Z")],
        start_page_token="SPT-1",
    )
    drive.sync_drive(conn, seed, folder_id=TEST_FOLDER_ID, connector="drive")
    assert conn.execute(
        "SELECT tombstoned FROM document_sources WHERE source_id = 'x1'"
    ).fetchone()["tombstoned"] == 0

    def _no_sweep(*a, **k):
        raise AssertionError("sweep_vanished_sources called on the incremental path")
    monkeypatch.setattr(drive._common, "sweep_vanished_sources", _no_sweep)

    service = _FakeDriveService(changes_pages=[{
        "changes": [{"fileId": "x1", "removed": True}],
        "newStartPageToken": "SPT-2",
    }])

    stats = drive.sync_drive(conn, service, folder_id=TEST_FOLDER_ID, connector="drive")

    assert stats["tombstoned"] == 1
    assert conn.execute(
        "SELECT tombstoned FROM document_sources WHERE source_id = 'x1'"
    ).fetchone()["tombstoned"] == 1


def test_sync_drive_incremental_trashed_file_also_tombstones(conn):
    """§1b / §3.3: `file.trashed == true` with `removed` NOT set is a
    distinct 'gone' signal - it must tombstone too."""
    seed = _FakeDriveService(
        content={"y1": b"# Y1\n\nbody\n"},
        listing=[_gdoc("y1", "2026-08-01T00:00:00.000Z")],
        start_page_token="SPT-1",
    )
    drive.sync_drive(conn, seed, folder_id=TEST_FOLDER_ID, connector="drive")

    trashed_file = {**_gdoc("y1", "2026-09-01T00:00:00.000Z"), "trashed": True}
    service = _FakeDriveService(changes_pages=[{
        "changes": [{"fileId": "y1", "file": trashed_file}],   # `removed` deliberately absent
        "newStartPageToken": "SPT-2",
    }])

    stats = drive.sync_drive(conn, service, folder_id=TEST_FOLDER_ID, connector="drive")

    assert stats["tombstoned"] == 1
    assert conn.execute(
        "SELECT tombstoned FROM document_sources WHERE source_id = 'y1'"
    ).fetchone()["tombstoned"] == 1


def test_sync_drive_incremental_normal_change_processes_including_a_shortcut(conn):
    """§3.3: a change with neither flag goes through process_drive_file,
    shortcut resolution included, exactly as the full path does."""
    db.set_connector_state(conn, "drive", "start_page_token", "SPT-old", NOW)

    target = {"id": "tgt", "name": "Target Doc", "mimeType": GDOC_MIME,
              "modifiedTime": "2026-01-01T00:00:00.000Z"}
    sc_file = {"id": "sc1", "name": "SC", "mimeType": _SHORTCUT,
              "modifiedTime": "2026-09-01T00:00:00.000Z",
              "shortcutDetails": {"targetId": "tgt"}}
    plain_file = _gdoc("p1", "2026-09-01T00:00:00.000Z")

    service = _FakeDriveService(
        content={"tgt": b"# Shortcut target\n\nbody\n", "p1": b"# Plain doc\n\nbody\n"},
        targets={"tgt": target},
        changes_pages=[{
            "changes": [
                {"fileId": "p1", "file": plain_file},
                {"fileId": "sc1", "file": sc_file},
            ],
            "newStartPageToken": "SPT-new",
        }],
    )

    stats = drive.sync_drive(conn, service, folder_id=TEST_FOLDER_ID, connector="drive")

    assert stats["pulled"] == 2
    assert ("get", {"fileId": "tgt", "fields": _TARGET_FIELDS}) in service.calls   # shortcut resolved
    assert {r["source_id"] for r in conn.execute("SELECT source_id FROM document_sources")} == {"p1", "sc1"}


def test_sync_drive_incremental_persists_only_the_final_pages_new_token(conn):
    """§1a / §3.3: multi-page changes.list - every page consumed, and only
    the final page's newStartPageToken is what gets stored."""
    db.set_connector_state(conn, "drive", "start_page_token", "SPT-0", NOW)
    service = _FakeDriveService(
        content={"m1": b"# M1\n\nbody\n", "m2": b"# M2\n\nbody\n"},
        changes_pages=[
            {"changes": [{"fileId": "m1", "file": _gdoc("m1", "2026-09-01T00:00:00.000Z")}],
             "nextPageToken": "page-2"},                    # no newStartPageToken on a non-final page
            {"changes": [{"fileId": "m2", "file": _gdoc("m2", "2026-09-01T00:00:00.000Z")}],
             "newStartPageToken": "SPT-FINAL"},
        ],
    )

    stats = drive.sync_drive(conn, service, folder_id=TEST_FOLDER_ID, connector="drive")

    assert stats["pulled"] == 2
    assert len([c for c in service.calls if c[0] == "changes.list"]) == 2
    assert db.get_connector_state(conn, "drive", "start_page_token") == "SPT-FINAL"


def test_sync_drive_incremental_httperror_falls_back_to_full_and_replaces_the_bad_cursor(conn, monkeypatch):
    """§3.4: any HttpError from changes.list -> discard the cursor, run the
    full path (which mints a fresh one). Assert the fallback ran and the
    invalid token is gone."""
    db.set_connector_state(conn, "drive", "start_page_token", "SPT-INVALID", NOW)

    ran = []
    real_full = drive._sync_drive_full
    def _spy_full(*a, **k):
        ran.append("full")
        return real_full(*a, **k)
    monkeypatch.setattr(drive_sync, "_sync_drive_full", _spy_full)

    service = _FakeDriveService(
        content={"f1": b"# F1\n\nbody\n"},
        listing=[_gdoc("f1", "2026-08-01T00:00:00.000Z")],
        start_page_token="SPT-REBUILT",
        changes_pages=[_http_error(410, "Gone")],      # cursor rejected
    )

    stats = drive.sync_drive(conn, service, folder_id=TEST_FOLDER_ID, connector="drive")

    assert ran == ["full"]                              # fallback actually executed
    assert stats["pulled"] == 1
    assert db.get_connector_state(conn, "drive", "start_page_token") == "SPT-REBUILT"   # not SPT-INVALID


def test_sync_drive_incremental_non_http_error_also_falls_back_to_full(conn, monkeypatch):
    """Adversarial review finding 3: after retries, googleapiclient re-raises
    transport failures (ConnectionError / ssl.SSLError / socket.timeout /
    httplib2.ServerNotFoundError) DIRECTLY - not wrapped in HttpError - so
    the old `except HttpError` was letting them escape the fallback. A
    non-HttpError from changes.list must trigger the same full-sync
    fallback that an HttpError does."""
    db.set_connector_state(conn, "drive", "start_page_token", "SPT-stale", NOW)

    ran = []
    real_full = drive._sync_drive_full
    def _spy_full(*a, **k):
        ran.append("full")
        return real_full(*a, **k)
    monkeypatch.setattr(drive_sync, "_sync_drive_full", _spy_full)

    service = _FakeDriveService(
        content={"f1": b"# F1\n\nbody\n"},
        listing=[_gdoc("f1", "2026-08-01T00:00:00.000Z")],
        start_page_token="SPT-rebuilt",
        changes_pages=[ConnectionError("simulated sustained network failure")],
    )

    stats = drive.sync_drive(conn, service, folder_id=TEST_FOLDER_ID, connector="drive")

    assert ran == ["full"]                              # fallback ran despite non-HttpError
    assert stats["pulled"] == 1
    assert db.get_connector_state(conn, "drive", "start_page_token") == "SPT-rebuilt"  # bad cursor replaced


def test_sync_drive_cursor_mint_failure_does_not_lose_an_otherwise_successful_run(conn, monkeypatch):
    """Adversarial review finding 4: getStartPageToken() blipping on the last
    line of _sync_drive_full must NOT throw away an otherwise-fully-
    successful run - every file was already fetched, hashed, committed and
    the sweep already ran. No exception out of sync_drive; stats returned;
    just no cursor persisted (so the next run does a full sync)."""
    service = _FakeDriveService(
        content={"a": b"# A\n\naaa\n", "b": b"# B\n\nbbb\n"},
        listing=[_gdoc("a", "2026-01-01T00:00:00.000Z"), _gdoc("b", "2026-01-01T00:00:00.000Z")],
    )

    class _BoomChanges:
        def getStartPageToken(self):
            raise ConnectionError("blip while minting the Changes API cursor")
    monkeypatch.setattr(service, "changes", lambda: _BoomChanges())

    stats = drive.sync_drive(conn, service, folder_id=TEST_FOLDER_ID, connector="drive")   # fresh conn -> full path

    assert stats["pulled"] == 2 and stats["errors"] == 0         # results intact, no exception raised
    assert conn.execute(
        "SELECT COUNT(*) FROM document_sources WHERE tombstoned = 0"
    ).fetchone()[0] == 2
    assert db.get_connector_state(conn, "drive", "start_page_token") is None  # no cursor persisted


def test_sync_drive_incremental_changes_list_never_sets_shared_drive_flags(conn):
    """§3.3: mirrors the existing files.list scope test - the Shared-Drive
    flags must not be set on changes.list either."""
    db.set_connector_state(conn, "drive", "start_page_token", "SPT-old", NOW)
    service = _FakeDriveService(changes_pages=[{"changes": [], "newStartPageToken": "SPT-new"}])

    drive.sync_drive(conn, service, folder_id=TEST_FOLDER_ID, connector="drive")

    (_, kwargs) = next(c for c in service.calls if c[0] == "changes.list")
    assert "includeItemsFromAllDrives" not in kwargs
    assert "supportsAllDrives" not in kwargs
    assert kwargs["spaces"] == "drive"


def test_sync_drive_full_with_an_error_does_not_advance_the_cursor(conn, monkeypatch):
    """§3.2 gate: a full run that could not ingest every file must NOT mint
    or persist a cursor - the next run has to replay the full path."""
    service = _FakeDriveService(
        content={"ok": b"# OK\n\nbody\n", "bad": b"# BAD\n\nbody\n"},
        listing=[_gdoc("ok", "2026-08-01T00:00:00.000Z"), _gdoc("bad", "2026-08-01T00:00:00.000Z")],
        start_page_token="SPT-would-be",
    )

    real_process = drive.process_drive_file

    def _process(conn_, svc, file_meta, *, now):
        if file_meta["id"] == "bad":
            raise RuntimeError("simulated fetch failure")
        return real_process(conn_, svc, file_meta, now=now)
    monkeypatch.setattr(drive_sync, "process_drive_file", _process)

    stats = drive.sync_drive(conn, service, folder_id=TEST_FOLDER_ID, connector="drive")

    assert stats["errors"] == 1
    assert ("changes.getStartPageToken", {}) not in service.calls   # never minted
    assert db.get_connector_state(conn, "drive", "start_page_token") is None

    # so a second run still takes the full path, not incremental
    monkeypatch.undo()
    service.calls.clear()
    second = drive.sync_drive(conn, service, folder_id=TEST_FOLDER_ID, connector="drive")
    assert any(c[0] == "list" for c in service.calls)               # full listing again
    assert not any(c[0] == "changes.list" for c in service.calls)
    assert second["errors"] == 0
    assert db.get_connector_state(conn, "drive", "start_page_token") == "SPT-would-be"


def test_sync_drive_incremental_with_an_error_does_not_advance_the_cursor(conn, monkeypatch):
    """§3.3 gate: an incremental run where a change fails leaves the stored
    token exactly where it was, so the next run replays the same batch."""
    db.set_connector_state(conn, "drive", "start_page_token", "SPT-original", NOW)

    def _process(conn_, svc, file_meta, *, now):
        raise RuntimeError("simulated fetch failure on the incremental path")
    monkeypatch.setattr(drive_sync, "process_drive_file", _process)

    service = _FakeDriveService(changes_pages=[{
        "changes": [{"fileId": "c1", "file": _gdoc("c1", "2026-09-01T00:00:00.000Z")}],
        "newStartPageToken": "SPT-advanced",
    }])

    stats = drive.sync_drive(conn, service, folder_id=TEST_FOLDER_ID, connector="drive")

    assert stats["errors"] == 1
    assert db.get_connector_state(conn, "drive", "start_page_token") == "SPT-original"   # NOT "SPT-advanced"


# ---------------------------------------------------------------------
# 11b. _sync_drive_full: empty-listing is no longer flagged as an error
#      (issue #78 - the old adversarial-review-finding-6 guard existed
#      only to protect the now-disabled unscoped sweep from an empty
#      whole-Drive listing; a folder-scoped empty result is no longer
#      inherently suspicious, and with the sweep disabled regardless
#      there's nothing left for the guard to protect)
# ---------------------------------------------------------------------

def test_sync_drive_full_empty_listing_of_a_folder_is_no_longer_flagged_as_an_error(conn):
    service = _FakeDriveService(
        content={"a": b"# A\n\naaa\n", "b": b"# B\n\nbbb\n"},
        listing=[_gdoc("a", "2026-01-01T00:00:00.000Z"), _gdoc("b", "2026-01-01T00:00:00.000Z")],
        start_page_token="SPT-first",
    )
    drive._sync_drive_full(conn, service, folder_id=TEST_FOLDER_ID, connector="drive")
    assert conn.execute(
        "SELECT COUNT(*) FROM document_sources WHERE tombstoned = 0"
    ).fetchone()[0] == 2
    assert db.get_connector_state(conn, "drive", "start_page_token") == "SPT-first"

    service.listing = []                                   # this folder is now empty
    service.calls.clear()
    stats = drive._sync_drive_full(conn, service, folder_id=TEST_FOLDER_ID, connector="drive")

    assert stats["errors"] == 0
    assert stats["tombstoned"] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM document_sources WHERE tombstoned = 1"
    ).fetchone()[0] == 0                                   # nothing swept (sweep is disabled regardless)
    # errors == 0 -> the cursor-mint gate now runs (unlike the old
    # guard-tripped path, which never re-minted at all)
    assert any(c[0] == "changes.getStartPageToken" for c in service.calls)


def test_sync_drive_full_empty_listing_on_a_true_first_run_is_not_treated_as_suspicious(conn):
    """Control: an empty listing when the connector has NO previously-active
    sources is a legitimate first run - proceed normally, no error, and a
    fresh cursor is still minted."""
    service = _FakeDriveService(listing=[], start_page_token="SPT-1")

    stats = drive._sync_drive_full(conn, service, folder_id=TEST_FOLDER_ID, connector="drive")

    assert stats["errors"] == 0
    assert stats["tombstoned"] == 0
    assert stats["pulled"] == 0
    assert db.get_connector_state(conn, "drive", "start_page_token") == "SPT-1"


# ---------------------------------------------------------------------
# 12. retry/backoff: num_retries wired to every Drive API call site
#     (docs/J11_PHASE3_DESIGN.md §3.2, §5)
# ---------------------------------------------------------------------

def _execute_kwargs_for(service: _FakeDriveService, execute_name: str) -> dict | None:
    """The kwargs .execute() was called with for a given call site (e.g.
    'list.execute'), or None if that site's .execute() was never reached."""
    for name, kwargs in service.calls:
        if name == execute_name:
            return kwargs
    return None


def test_list_drive_files_execute_uses_num_retries():
    service = _FakeDriveService(listing=[])
    drive.list_drive_files(service)
    assert _execute_kwargs_for(service, "list.execute") == {
        "num_retries": drive.DRIVE_API_NUM_RETRIES
    }


def test_fetch_file_bytes_export_media_execute_uses_num_retries():
    service = _FakeDriveService(content={"d1": b"# Doc\n\nbody\n"})
    drive.fetch_file_bytes(service, {"id": "d1", "name": "Doc", "mimeType": GDOC_MIME})
    assert _execute_kwargs_for(service, "export_media.execute") == {
        "num_retries": drive.DRIVE_API_NUM_RETRIES
    }


def test_fetch_file_bytes_get_media_execute_uses_num_retries():
    service = _FakeDriveService(content={"x1": _minimal_pdf_bytes("pdf body")})
    drive.fetch_file_bytes(service, {"id": "x1", "name": "spec.pdf", "mimeType": PDF_MIME})
    assert _execute_kwargs_for(service, "get_media.execute") == {
        "num_retries": drive.DRIVE_API_NUM_RETRIES
    }


def test_resolve_shortcut_target_get_execute_uses_num_retries():
    target = {"id": "t1", "name": "T", "mimeType": GDOC_MIME, "modifiedTime": "t"}
    service = _FakeDriveService(targets={"t1": target})
    drive.resolve_shortcut_target(
        service, {"id": "sc1", "name": "SC", "mimeType": _SHORTCUT,
                  "shortcutDetails": {"targetId": "t1"}}
    )
    assert _execute_kwargs_for(service, "get.execute") == {
        "num_retries": drive.DRIVE_API_NUM_RETRIES
    }


def test_sync_drive_full_get_start_page_token_execute_uses_num_retries(conn):
    service = _FakeDriveService(
        content={"g1": b"# G1\n\nbody\n"},
        listing=[_gdoc("g1", "2026-08-01T00:00:00.000Z")],
    )
    drive.sync_drive(conn, service, folder_id=TEST_FOLDER_ID, connector="drive")   # fresh db -> full path -> mints a token
    assert _execute_kwargs_for(service, "changes.getStartPageToken.execute") == {
        "num_retries": drive.DRIVE_API_NUM_RETRIES
    }


def test_sync_drive_incremental_changes_list_execute_uses_num_retries(conn):
    db.set_connector_state(conn, "drive", "start_page_token", "SPT-old", NOW)
    service = _FakeDriveService(changes_pages=[{"changes": [], "newStartPageToken": "SPT-new"}])
    drive.sync_drive(conn, service, folder_id=TEST_FOLDER_ID, connector="drive")   # stored token -> incremental path
    assert _execute_kwargs_for(service, "changes.list.execute") == {
        "num_retries": drive.DRIVE_API_NUM_RETRIES
    }


# --- §5 regression checks: num_retries on a call site does not disturb its
#     existing specific-error handling ---

def test_doc_over_export_cap_still_skips_cleanly_with_num_retries_present(conn, caplog):
    service = _FakeDriveService(content={"big": _export_size_error()})
    meta = {"id": "big", "name": "Huge doc", "mimeType": GDOC_MIME,
            "modifiedTime": "2026-01-01T00:00:00.000Z"}

    with caplog.at_level("WARNING"):
        status = drive.process_drive_file(conn, service, meta, now=NOW)

    assert status == "skipped"
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    assert _execute_kwargs_for(service, "export_media.execute") == {
        "num_retries": drive.DRIVE_API_NUM_RETRIES
    }
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("10 MB" in r.message and "Huge doc" in r.message for r in warnings)


def test_vanished_shortcut_target_still_warns_and_returns_none_with_num_retries_present(conn, caplog):
    service = _FakeDriveService(targets={"gone": _http_error(404)})
    meta = {"id": "sc4", "name": "Shortcut to a deleted file", "mimeType": _SHORTCUT,
            "modifiedTime": "t", "shortcutDetails": {"targetId": "gone"}}

    with caplog.at_level("WARNING"):
        status = drive.process_drive_file(conn, service, meta, now=NOW)

    assert status == "skipped"
    assert _execute_kwargs_for(service, "get.execute") == {
        "num_retries": drive.DRIVE_API_NUM_RETRIES
    }
    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any("vanished or inaccessible" in m for m in warnings)


def test_list_drive_folders_execute_uses_num_retries(conn):
    service = _FakeDriveService(folders_by_parent={"root": []})
    drive.list_drive_folders(service)
    assert _execute_kwargs_for(service, "list.execute") == {
        "num_retries": drive.DRIVE_API_NUM_RETRIES
    }


# ---------------------------------------------------------------------
# 13. OAuth re-auth UX (Rank 2 finding): the ~weekly Testing-mode token
#     expiry must not be a silent hang (headless) or an unexplained
#     browser popup (interactive)
# ---------------------------------------------------------------------

class _ExpiredCreds:
    """Stand-in for a loaded-from-disk token whose refresh token has died
    (the 7-day Testing-mode case)."""
    expired = True
    refresh_token = "dead-refresh-token"
    valid = False

    def refresh(self, request):
        from google.auth.exceptions import RefreshError
        raise RefreshError("Token has been expired or revoked.")

    def to_json(self):
        return "{}"


@pytest.fixture
def _expired_token(tmp_path, monkeypatch):
    """A token file on disk + monkeypatched loaders so get_drive_service
    takes the 're-auth needed' branch without touching real Google."""
    import google.oauth2.credentials as creds_mod

    tok = tmp_path / ".google_drive_token.json"
    tok.write_text('{"refresh_token": "x"}')
    monkeypatch.setattr(
        creds_mod.Credentials, "from_authorized_user_file",
        staticmethod(lambda *a, **k: _ExpiredCreds()),
    )
    monkeypatch.setattr(drive_auth, "_client_config_from_env", lambda: {"installed": {}})
    return tok


def test_get_drive_service_non_interactive_fails_fast_instead_of_hanging(_expired_token):
    with pytest.raises(RuntimeError, match="re-authorization"):
        drive.get_drive_service(token_path=_expired_token, allow_interactive_auth=False)


def test_get_drive_service_interactive_prints_a_clear_expiry_message_first(
    _expired_token, monkeypatch, capsys
):
    import google_auth_oauthlib.flow as flow_mod
    from googleapiclient import discovery

    called = {}

    class _FakeFlow:
        @classmethod
        def from_client_config(cls, *a, **k):
            return cls()

        def run_local_server(self, **kwargs):
            called["run_local_server_kwargs"] = kwargs
            return _ExpiredCreds()   # any object; build() is stubbed below

    monkeypatch.setattr(flow_mod, "InstalledAppFlow", _FakeFlow)
    monkeypatch.setattr(discovery, "build", lambda *a, **k: "FAKE_SERVICE")

    svc = drive.get_drive_service(token_path=_expired_token, allow_interactive_auth=True)

    out = capsys.readouterr().out
    assert "authorization has expired" in out
    assert "browser window will open" in out
    # and it no longer waits forever
    assert called["run_local_server_kwargs"].get("timeout_seconds")
    assert svc == "FAKE_SERVICE"


# ---------------------------------------------------------------------
# 14. Token refresh retries a transient network drop (real bug report: a
#     tester's sync failed on a raw ChunkedEncodingError/ConnectionResetError
#     mid-refresh) rather than treating it like a real auth rejection.
# ---------------------------------------------------------------------

class _FlakyThenOkCreds:
    """Raises a transient network error from `.refresh()` a fixed number
    of times, then succeeds - the shape a real connection drop mid-token-
    refresh takes (not a RefreshError - the request never got a response
    at all)."""

    expired = True
    refresh_token = "x"

    def __init__(self, fail_times, exc_factory):
        self._fail_times = fail_times
        self._exc_factory = exc_factory
        self.calls = 0
        self.valid = False

    def refresh(self, request):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._exc_factory()
        self.valid = True


def _chunked_encoding_error():
    import requests
    return requests.exceptions.ChunkedEncodingError(
        "Connection broken: ConnectionResetError(10054, 'An existing connection was forcibly "
        "closed by the remote host')"
    )


def test_refresh_retries_a_transient_network_error_then_succeeds(monkeypatch):
    monkeypatch.setattr(drive_auth.time, "sleep", lambda seconds: None)
    creds = _FlakyThenOkCreds(fail_times=2, exc_factory=_chunked_encoding_error)

    drive_auth._refresh_with_retry(creds)

    assert creds.calls == 3
    assert creds.valid is True


def test_refresh_gives_up_after_max_retries(monkeypatch):
    import requests

    monkeypatch.setattr(drive_auth.time, "sleep", lambda seconds: None)
    creds = _FlakyThenOkCreds(fail_times=99, exc_factory=_chunked_encoding_error)

    with pytest.raises(requests.exceptions.ChunkedEncodingError):
        drive_auth._refresh_with_retry(creds)

    assert creds.calls == drive_auth._TOKEN_REFRESH_MAX_RETRIES + 1


def test_refresh_does_not_retry_a_real_refresh_error(monkeypatch):
    """A revoked/expired refresh token is a real rejection, not a
    network hiccup - must propagate immediately, not spend two retries
    (with real sleeps) on something retrying can never fix."""
    from google.auth.exceptions import RefreshError

    def _boom():
        raise RefreshError("invalid_grant")

    def _must_not_sleep(seconds):
        raise AssertionError("must not sleep/retry a real RefreshError")

    monkeypatch.setattr(drive_auth.time, "sleep", _must_not_sleep)
    creds = _FlakyThenOkCreds(fail_times=1, exc_factory=_boom)

    with pytest.raises(RefreshError):
        drive_auth._refresh_with_retry(creds)

    assert creds.calls == 1


def _seed_active_drive_source(conn, file_id: str) -> None:
    db.insert_document(
        conn, content_hash=f"hash-{file_id}", normalized_markdown=file_id, first_seen_at=NOW, last_ingested_at=NOW,
    )
    db.insert_document_source(
        conn, source_id=file_id, connector=drive.CONNECTOR_NAME, content_hash=f"hash-{file_id}", source_last_seen=NOW,
    )


def test_owned_source_ids_keeps_only_files_another_tracked_folder_still_lists(conn):
    service = _FakeDriveService(files_by_parent={
        "removed": [_file_entry("only-here"), _file_entry("shared"), _folder_entry("nested")],
        "nested": [_file_entry("deep")],
        "kept": [_file_entry("shared"), _folder_entry("kept-sub")],
        "kept-sub": [_file_entry("other")],
    })
    # "moved-away" was synced from the removed folder but has since been
    # moved or deleted in Drive, so no listing returns it any more.
    for file_id in ("only-here", "shared", "deep", "other", "moved-away"):
        _seed_active_drive_source(conn, file_id)

    owned = drive.DriveConnector("removed", service=service).owned_source_ids(conn, ["kept"])

    assert owned == {"only-here", "deep", "moved-away"}


def test_removing_the_last_drive_folder_takes_every_drive_file_without_calling_drive(conn):
    service = _FakeDriveService(list_exc=AssertionError("no listing needed"))
    for file_id in ("a", "moved-away"):
        _seed_active_drive_source(conn, file_id)

    owned = drive.DriveConnector("removed", service=service).owned_source_ids(conn, [])

    assert owned == {"a", "moved-away"}


def test_a_drive_sync_looks_up_the_folder_name_for_the_sources_page(conn):
    service = _FakeDriveService(listing=[], targets={TEST_FOLDER_ID: {"id": TEST_FOLDER_ID, "name": "Team docs"}})
    connector = drive.DriveConnector(TEST_FOLDER_ID, service=service)

    connector.sync(conn)

    assert connector.display_name == "Team docs"


def test_a_failed_folder_name_lookup_never_fails_the_sync(conn):
    service = _FakeDriveService(listing=[], targets={TEST_FOLDER_ID: _http_error(404)})
    connector = drive.DriveConnector(TEST_FOLDER_ID, service=service)

    stats = connector.sync(conn)

    assert stats["errors"] == 0
    assert connector.display_name is None
