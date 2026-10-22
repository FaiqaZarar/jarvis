"""J-04 acceptance check: running the filesystem connector on a test folder
produces correct document_sources rows with no duplicate documents for
identical files."""

import os
from pathlib import Path

import pytest

from jarvis_server.internal import db
from jarvis_server.connectors.filesystem import (
    FilesystemConnector,
    guess_mime,
    sync_filesystem,
    walk_directory,
)
from jarvis_server.internal.migrate import run_migrations
from jarvis_server.internal.normalizer.converters.docx import DocxNormalizer


@pytest.fixture
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    connection = db.get_connection(db_path)
    yield connection
    connection.close()


@pytest.fixture
def test_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "watched_root"
    folder.mkdir()
    return folder


def test_identical_content_dedups_to_one_document(conn, test_folder: Path):
    """The core acceptance criterion: two files with identical content
    produce one Document with two provenance links, not two Documents."""
    (test_folder / "a.md").write_text("# Same content\n\nHello.\n")
    (test_folder / "b.md").write_text("# Same content\n\nHello.\n")

    stats = sync_filesystem(conn, str(test_folder))

    assert stats["pulled"] == 2
    assert stats["errors"] == 0

    documents = conn.execute("SELECT * FROM documents").fetchall()
    assert len(documents) == 1

    sources = db.get_document_sources_for_document(conn, documents[0]["content_hash"])
    assert len(sources) == 2


def test_different_content_produces_separate_documents(conn, test_folder: Path):
    (test_folder / "one.md").write_text("# First\n")
    (test_folder / "two.md").write_text("# Second\n")

    sync_filesystem(conn, str(test_folder))

    documents = conn.execute("SELECT * FROM documents").fetchall()
    assert len(documents) == 2


def test_source_id_is_absolute_path(conn, test_folder: Path):
    (test_folder / "note.txt").write_text("hello\n")

    sync_filesystem(conn, str(test_folder))

    rows = conn.execute("SELECT source_id FROM document_sources").fetchall()
    assert len(rows) == 1
    assert Path(rows[0]["source_id"]).is_absolute()
    assert rows[0]["source_id"].endswith("note.txt")


def test_dotfiles_are_skipped(conn, test_folder: Path):
    (test_folder / "visible.md").write_text("# Visible\n")
    (test_folder / ".hidden.md").write_text("# Hidden\n")

    files = list(walk_directory(test_folder))
    names = [f.name for f in files]

    assert "visible.md" in names
    assert ".hidden.md" not in names


def test_symlink_pointing_outside_the_root_is_skipped(tmp_path: Path, test_folder: Path):
    """Real, confirmed gap: os.walk(followlinks=False) only stops
    *descending into* a symlinked directory - a symlinked file was still
    yielded, read, and ingested even though its target lives outside the
    folder the user actually pointed Jarvis at (e.g. a synced/shared
    folder containing a symlink to ~/.aws/credentials)."""
    outside_secret = tmp_path / "outside_root_secret.md"
    outside_secret.write_text("# Not part of this folder\n")
    link = test_folder / "innocuous.md"
    try:
        os.symlink(outside_secret, link)
    except OSError:
        pytest.skip("creating a file symlink needs elevated privileges/Developer Mode here")

    files = list(walk_directory(test_folder))

    assert link not in files
    assert all(f.resolve().is_relative_to(test_folder.resolve()) for f in files)


def test_symlink_pointing_inside_the_root_is_still_ingested(tmp_path: Path, test_folder: Path):
    """The fix above must not reject every symlink - only ones that
    escape the watched root."""
    real_file = test_folder / "real.md"
    real_file.write_text("# Real\n")
    link = test_folder / "alias.md"
    try:
        os.symlink(real_file, link)
    except OSError:
        pytest.skip("creating a file symlink needs elevated privileges/Developer Mode here")

    files = list(walk_directory(test_folder))

    assert link in files


# ---------------------------------------------------------------------
# MIME type (issue #51: the search file_type filter needs this column)
# ---------------------------------------------------------------------

def test_guess_mime_covers_every_supported_extension():
    assert guess_mime(Path("notes.md")) == "text/markdown"
    assert guess_mime(Path("notes.txt")) == "text/plain"
    assert guess_mime(Path("report.pdf")) == "application/pdf"
    assert guess_mime(Path("memo.docx")) == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert guess_mime(Path("data.csv")) == "text/csv"
    assert guess_mime(Path("page.html")) == "text/html"
    assert guess_mime(Path("mystery.zzz")) is None   # unknown extension -> None, column is nullable


def test_mime_is_populated_on_ingest(conn, test_folder: Path):
    (test_folder / "guide.md").write_text("# Guide\n\nSome markdown.\n")
    (test_folder / "log.txt").write_text("plain text log line\n")

    sync_filesystem(conn, str(test_folder))

    by_name = {
        Path(row["source_id"]).name: row["mime"]
        for row in conn.execute("SELECT source_id, mime FROM document_sources").fetchall()
    }
    assert by_name == {"guide.md": "text/markdown", "log.txt": "text/plain"}


def test_mime_updates_when_an_extension_changes_on_rename(conn, test_folder: Path):
    original = test_folder / "note.txt"
    original.write_text("same bytes, extension will change\n")
    sync_filesystem(conn, str(test_folder))
    assert conn.execute("SELECT mime FROM document_sources").fetchone()["mime"] == "text/plain"

    original.rename(test_folder / "note.md")   # inode-preserving rename
    sync_filesystem(conn, str(test_folder))

    rows = conn.execute("SELECT source_id, mime FROM document_sources WHERE tombstoned = 0").fetchall()
    assert len(rows) == 1
    assert Path(rows[0]["source_id"]).name == "note.md"
    assert rows[0]["mime"] == "text/markdown"


def test_dotdirs_are_not_descended_into(conn, test_folder: Path):
    hidden_dir = test_folder / ".git"
    hidden_dir.mkdir()
    (hidden_dir / "config.md").write_text("# Should not be picked up\n")
    (test_folder / "real.md").write_text("# Real content\n")

    files = list(walk_directory(test_folder))
    names = [f.name for f in files]

    assert "real.md" in names
    assert "config.md" not in names


def test_dependency_and_build_dirs_are_not_descended_into_even_without_a_gitignore(
    conn, test_folder: Path,
):
    for dirname in ("node_modules", "venv", "__pycache__", "site-packages", "dist", "build"):
        nested = test_folder / "app" / dirname / "pkg"
        nested.mkdir(parents=True)
        (nested / "README.md").write_text("# Dependency readme\n")
    (test_folder / "app" / "notes.md").write_text("# Real content\n")

    files = list(walk_directory(test_folder))

    assert [f.name for f in files] == ["notes.md"]


def test_sync_reports_per_file_progress_against_the_walked_total(conn, test_folder: Path):
    (test_folder / "a.md").write_text("# A\n")
    (test_folder / "b.md").write_text("# B\n")
    (test_folder / "c.md").write_text("# C\n")
    calls = []

    sync_filesystem(conn, str(test_folder), on_progress=lambda *args: calls.append(args))

    assert calls == [(1, 3, 0), (2, 3, 0), (3, 3, 0)]


def test_gitignore_patterns_are_respected(conn, test_folder: Path):
    (test_folder / ".gitignore").write_text("ignored/\n*.secret\n")
    ignored_dir = test_folder / "ignored"
    ignored_dir.mkdir()
    (ignored_dir / "skip_me.md").write_text("# Should be ignored\n")
    (test_folder / "keep.md").write_text("# Should be kept\n")
    (test_folder / "creds.secret").write_text("shh\n")

    files = list(walk_directory(test_folder))
    names = {f.name for f in files}

    assert "keep.md" in names
    assert "skip_me.md" not in names
    assert "creds.secret" not in names


def test_unsupported_extensions_are_skipped(conn, test_folder: Path):
    (test_folder / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (test_folder / "doc.md").write_text("# A real doc\n")

    stats = sync_filesystem(conn, str(test_folder))

    assert stats["pulled"] == 1
    assert stats["skipped"] == 0  # .png never yielded by walk_directory at all
    documents = conn.execute("SELECT * FROM documents").fetchall()
    assert len(documents) == 1


def test_vanished_file_is_tombstoned_on_resync(conn, test_folder: Path):
    file_a = test_folder / "temp.md"
    file_a.write_text("# Temporary\n")
    (test_folder / "permanent.md").write_text("# Permanent\n")

    stats_first = sync_filesystem(conn, str(test_folder))
    assert stats_first["pulled"] == 2
    assert stats_first["tombstoned"] == 0

    file_a.unlink()

    stats_second = sync_filesystem(conn, str(test_folder))
    assert stats_second["pulled"] == 1
    assert stats_second["tombstoned"] == 1

    rows = conn.execute(
        "SELECT tombstoned FROM document_sources WHERE source_id LIKE '%temp.md'"
    ).fetchall()
    assert rows[0]["tombstoned"] == 1


# ---------------------------------------------------------------------
# Multi-folder: Phase B tombstone sweep is scoped to the synced root
# (issue #61 - syncing folder B must not tombstone folder A)
# ---------------------------------------------------------------------

def test_syncing_a_second_folder_does_not_tombstone_the_first(conn, tmp_path: Path):
    folder_a = tmp_path / "folder_a"
    folder_a.mkdir()
    (folder_a / "alpha.md").write_text("# Alpha\n\naardvarks and Antarctica\n")
    (folder_a / "beta.md").write_text("# Beta\n\nbasilisks and Belgium\n")
    folder_b = tmp_path / "folder_b"
    folder_b.mkdir()
    (folder_b / "gamma.md").write_text("# Gamma\n\ngorillas and Greenland\n")
    (folder_b / "delta.md").write_text("# Delta\n\ndolphins and Denmark\n")

    first = sync_filesystem(conn, str(folder_a))          # default connector, no override
    assert first["pulled"] == 2 and first["tombstoned"] == 0

    second = sync_filesystem(conn, str(folder_b))          # same connector name
    assert second["pulled"] == 2
    assert second["tombstoned"] == 0                       # <- folder A left alone

    live = {
        Path(r["source_id"]).name
        for r in conn.execute("SELECT source_id FROM document_sources WHERE tombstoned = 0")
    }
    assert live == {"alpha.md", "beta.md", "gamma.md", "delta.md"}


def test_empty_folder_sync_does_not_tombstone_another_folder(conn, tmp_path: Path):
    """Phase B still runs on a zero-file pull; scoped to the (empty) root
    it correctly tombstones nothing outside it."""
    folder_a = tmp_path / "folder_a"
    folder_a.mkdir()
    (folder_a / "keep.md").write_text("# Keep\n\ncontent that must survive\n")
    sync_filesystem(conn, str(folder_a))

    empty = tmp_path / "empty"
    empty.mkdir()
    stats = sync_filesystem(conn, str(empty))
    assert stats["pulled"] == 0
    assert stats["tombstoned"] == 0

    row = conn.execute(
        "SELECT tombstoned FROM document_sources WHERE source_id LIKE '%keep.md'"
    ).fetchone()
    assert row["tombstoned"] == 0


def test_sibling_folder_with_shared_name_prefix_is_not_swept(conn, tmp_path: Path):
    """`.../notes` and `.../notes-archive` share a string prefix; the
    trailing separator in the root check must keep them independent."""
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "n.md").write_text("# Notes\n\nkeep me\n")
    archive = tmp_path / "notes-archive"
    archive.mkdir()
    (archive / "a.md").write_text("# Archive\n\nkeep me too\n")

    sync_filesystem(conn, str(notes))
    stats = sync_filesystem(conn, str(archive))
    assert stats["tombstoned"] == 0

    row = conn.execute(
        "SELECT tombstoned FROM document_sources WHERE source_id LIKE '%notes%n.md'"
    ).fetchone()
    assert row["tombstoned"] == 0


def test_vanished_file_still_tombstoned_within_its_own_root(conn, tmp_path: Path):
    """The scoping fix must not break the normal case: a file that
    disappears from the folder it belongs to is still tombstoned."""
    folder = tmp_path / "watched"
    folder.mkdir()
    gone = folder / "temp.md"
    gone.write_text("# Temp\n")
    (folder / "stays.md").write_text("# Stays\n")
    sync_filesystem(conn, str(folder))

    gone.unlink()
    stats = sync_filesystem(conn, str(folder))
    assert stats["tombstoned"] == 1
    row = conn.execute(
        "SELECT tombstoned FROM document_sources WHERE source_id LIKE '%temp.md'"
    ).fetchone()
    assert row["tombstoned"] == 1


def test_reappearing_file_is_untombstoned_on_resync(conn, test_folder: Path):
    """A file that vanishes and comes back should get un-tombstoned, and
    since its content hash is unchanged, its Document is already cached -
    no re-embed needed later (spec §5.2)."""
    file_a = test_folder / "temp.md"
    file_a.write_text("# Temporary\n")

    sync_filesystem(conn, str(test_folder))
    file_a.unlink()
    sync_filesystem(conn, str(test_folder))

    file_a.write_text("# Temporary\n")  # same content, reappears
    sync_filesystem(conn, str(test_folder))

    rows = conn.execute(
        "SELECT tombstoned FROM document_sources WHERE source_id LIKE '%temp.md'"
    ).fetchall()
    assert rows[0]["tombstoned"] == 0


def test_unchanged_resync_produces_no_duplicate_rows(conn, test_folder: Path):
    (test_folder / "stable.md").write_text("# Stable content\n")

    sync_filesystem(conn, str(test_folder))
    sync_filesystem(conn, str(test_folder))

    documents = conn.execute("SELECT * FROM documents").fetchall()
    sources = conn.execute("SELECT * FROM document_sources").fetchall()
    assert len(documents) == 1
    assert len(sources) == 1


def test_non_utf8_file_decodes_via_fallback(conn, test_folder: Path):
    """A file encoded in Latin-1 (not valid UTF-8) should still decode via
    the charset-normalizer fallback, not crash the sync."""
    latin1_path = test_folder / "latin1.txt"
    latin1_path.write_bytes("café".encode("latin-1"))

    stats = sync_filesystem(conn, str(test_folder))

    assert stats["errors"] == 0
    assert stats["pulled"] == 1


# ---------------------------------------------------------------------
# fast-path skip, rename detection, TCC PermissionError handling (§8.2)
# ---------------------------------------------------------------------

def test_unchanged_file_hits_fast_path_on_resync(conn, test_folder: Path):
    """An untouched file's (mtime, size) match on the second sync, so it
    should be skipped via the fast path rather than re-read/re-hashed."""
    (test_folder / "stable.md").write_text("# Stable\n")

    stats_first = sync_filesystem(conn, str(test_folder))
    assert stats_first["fast_skipped"] == 0
    assert stats_first["pulled"] == 1

    stats_second = sync_filesystem(conn, str(test_folder))
    assert stats_second["fast_skipped"] == 1
    assert stats_second["pulled"] == 1
    assert stats_second["errors"] == 0


def test_changed_content_bypasses_fast_path(conn, test_folder: Path):
    """A file whose content (and therefore mtime/size) changed must be
    fully re-read and re-hashed, not skipped."""
    target = test_folder / "changing.md"
    target.write_text("# Version one\n")
    sync_filesystem(conn, str(test_folder))

    target.write_text("# Version two, much longer content than before\n")
    stats = sync_filesystem(conn, str(test_folder))

    assert stats["fast_skipped"] == 0
    assert stats["pulled"] == 1

    documents = conn.execute("SELECT * FROM documents").fetchall()
    assert len(documents) == 2  # old content_hash retained, new one added


def test_renamed_file_repoints_existing_row_via_inode(conn, test_folder: Path):
    """Moving a file to a new path should be recognized as a rename (same
    st_dev/st_ino) and repoint the existing document_sources row, rather
    than tombstoning the old path and inserting a brand-new row."""
    old_path = test_folder / "old_name.md"
    old_path.write_text("# Renamed content\n")

    stats_first = sync_filesystem(conn, str(test_folder))
    assert stats_first["pulled"] == 1

    before = conn.execute("SELECT * FROM document_sources").fetchall()
    assert len(before) == 1
    original_content_hash = before[0]["content_hash"]

    new_path = test_folder / "new_name.md"
    os.rename(old_path, new_path)

    stats_second = sync_filesystem(conn, str(test_folder))
    assert stats_second["renamed"] == 1
    assert stats_second["tombstoned"] == 0

    rows = conn.execute("SELECT * FROM document_sources").fetchall()
    assert len(rows) == 1  # repointed in place, not a second row
    assert rows[0]["source_id"].endswith("new_name.md")
    assert rows[0]["content_hash"] == original_content_hash
    assert rows[0]["tombstoned"] == 0


def test_permission_error_is_actionable_and_does_not_crash_sync(
    conn, test_folder: Path, monkeypatch, caplog
):
    """A PermissionError on one file (e.g. macOS TCC) must not crash the
    whole sync, must log an actionable hint, and - like any other error -
    must suppress this run's tombstone sweep (spec §5.3)."""
    (test_folder / "blocked.md").write_text("# Blocked\n")
    (test_folder / "ok.md").write_text("# Fine\n")

    real_read_bytes = Path.read_bytes

    def fake_read_bytes(self: Path):
        if self.name == "blocked.md":
            raise PermissionError(13, "Permission denied", str(self))
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)

    with caplog.at_level("WARNING"):
        stats = sync_filesystem(conn, str(test_folder))

    assert stats["errors"] == 1
    assert stats["pulled"] == 1  # ok.md still made it through
    assert stats["tombstoned"] == 0
    assert any("Full Disk Access" in record.message for record in caplog.records)


def _minimal_docx_bytes(paragraph_text: str) -> bytes:
    """A real, valid, minimal .docx (zip + OOXML), built by hand so this
    test needs no extra dependency (python-docx isn't in requirements.txt -
    only markitdown[docx] is, which reads .docx, not writes it)."""
    import io
    import zipfile

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{paragraph_text}</w:t></w:r></w:p></w:body>"
        "</w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
    return buf.getvalue()


def test_docx_file_converts_end_to_end_through_sync(conn, test_folder: Path):
    """A real .docx must flow all the way through walk -> open_file ->
    Normalize -> DocxNormalizer -> hashing and land in the store. Guards
    against the markitdown[docx]/[pdf] extras regressing silently again
    (found missing during the J-03 normalizer redesign)."""
    (test_folder / "report.docx").write_bytes(
        _minimal_docx_bytes("Real docx content flowing through the connector.")
    )

    stats = sync_filesystem(conn, str(test_folder))

    assert stats["errors"] == 0
    assert stats["skipped"] == 0
    assert stats["pulled"] == 1

    documents = conn.execute("SELECT * FROM documents").fetchall()
    assert len(documents) == 1
    assert "Real docx content flowing through the connector." in documents[0]["normalized_markdown"]


def test_conversion_failure_is_counted_as_an_error_not_a_skip(conn, test_folder: Path, monkeypatch):
    """A converter that's actually dispatched and fails (e.g. magika's
    packaged-build MagikaError - issue #114) must land in stats["errors"],
    not stats["skipped"] - the two were previously indistinguishable,
    which made real ingest failures invisible in the sync summary
    (issue #115)."""
    monkeypatch.setattr(DocxNormalizer, "convert", lambda self, file: (None, "boom"))
    (test_folder / "report.docx").write_bytes(b"not a real docx, but never reached anyway")

    stats = sync_filesystem(conn, str(test_folder))

    assert stats["errors"] == 1
    assert stats["skipped"] == 0
    assert stats["pulled"] == 0
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def _minimal_pdf_bytes(paragraph_text: str) -> bytes:
    """A real, valid, minimal single-page PDF, built by hand so this test
    needs no extra dependency (reportlab/fpdf aren't in requirements.txt -
    only markitdown[pdf] is, which reads PDFs, not writes them). Computes
    exact xref byte offsets rather than relying on a lenient parser."""
    content_stream = (
        f"BT /F1 24 Tf 72 712 Td ({paragraph_text}) Tj ET".encode("latin-1")
    )

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 5 0 R >> >> "
        b"/MediaBox [0 0 612 792] /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(content_stream)
        + content_stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    buf = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(buf))
        buf += f"{i} 0 obj\n".encode("latin-1") + body + b"\nendobj\n"

    xref_offset = len(buf)
    buf += f"xref\n0 {len(objects) + 1}\n".encode("latin-1")
    buf += b"0000000000 65535 f \n"
    for off in offsets:
        buf += f"{off:010d} 00000 n \n".encode("latin-1")
    buf += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF"
    ).encode("latin-1")

    return bytes(buf)


def test_pdf_file_converts_end_to_end_through_sync(conn, test_folder: Path):
    """A real .pdf must flow all the way through walk -> open_file ->
    Normalize -> PDFNormalizer -> hashing and land in the store. Same
    real-conversion guard as the .docx test above, for the other format
    markitdown[docx,pdf] adds support for."""
    (test_folder / "report.pdf").write_bytes(
        _minimal_pdf_bytes("Real pdf content flowing through the connector.")
    )

    stats = sync_filesystem(conn, str(test_folder))

    assert stats["errors"] == 0
    assert stats["skipped"] == 0
    assert stats["pulled"] == 1

    documents = conn.execute("SELECT * FROM documents").fetchall()
    assert len(documents) == 1
    assert "Real pdf content flowing through the connector." in documents[0]["normalized_markdown"]


def test_csv_file_converts_end_to_end_through_sync(conn, test_folder: Path):
    """A real .csv flows walk -> open_file -> Normalize -> CsvNormalizer ->
    hashing and lands in the store as a Markdown table. No extra markitdown
    extra needed - the CSV converter is stdlib-only."""
    (test_folder / "roster.csv").write_text(
        "name,team,city\nAda Lovelace,analytical engines,London\n"
        "Grace Hopper,compilers,New York\n"
    )

    stats = sync_filesystem(conn, str(test_folder))

    assert stats["errors"] == 0
    assert stats["skipped"] == 0
    assert stats["pulled"] == 1

    row = conn.execute(
        "SELECT d.normalized_markdown, s.mime FROM documents d "
        "JOIN document_sources s ON d.content_hash = s.content_hash"
    ).fetchone()
    assert "Ada Lovelace" in row["normalized_markdown"]
    assert "| name | team | city |" in row["normalized_markdown"]   # rendered as a table
    assert row["mime"] == "text/csv"


def test_html_file_converts_end_to_end_through_sync(conn, test_folder: Path):
    """A real .html flows all the way through to a Markdown document in the
    store. beautifulsoup4 + markdownify come in transitively with
    markitdown[docx]; no requirements.txt change."""
    (test_folder / "notes.html").write_text(
        "<html><body><h1>Sprint Notes</h1>"
        "<p>Shipped the <b>search</b> tool. Next: connectors.</p>"
        "<ul><li>drive</li><li>notion</li></ul></body></html>"
    )

    stats = sync_filesystem(conn, str(test_folder))

    assert stats["errors"] == 0
    assert stats["skipped"] == 0
    assert stats["pulled"] == 1

    row = conn.execute(
        "SELECT d.normalized_markdown, s.mime FROM documents d "
        "JOIN document_sources s ON d.content_hash = s.content_hash"
    ).fetchone()
    assert "# Sprint Notes" in row["normalized_markdown"]
    assert "**search**" in row["normalized_markdown"]
    assert row["mime"] == "text/html"


# ---------------------------------------------------------------------
# Per-extension stats (issue #89 follow-on: file_type_stats)
# ---------------------------------------------------------------------

def test_unsupported_extension_is_recorded_as_ignored_in_ext_stats(conn, test_folder: Path):
    (test_folder / "image.xyz").write_bytes(b"not a real format")
    (test_folder / "doc.md").write_text("# A real doc\n")

    stats = sync_filesystem(conn, str(test_folder))

    entry = stats["ext_stats"][".xyz"]
    assert entry["supported"] is False
    assert entry["ignored"] == 1
    assert entry["tried"] == 0


def test_stored_file_is_recorded_as_tried_with_nonzero_bytes(conn, test_folder: Path):
    (test_folder / "doc.md").write_text("# A real doc with some content\n")

    stats = sync_filesystem(conn, str(test_folder))

    entry = stats["ext_stats"][".md"]
    assert entry["supported"] is True
    assert entry["tried"] == 1
    assert entry["bytes"] > 0


def test_fast_skipped_resync_does_not_double_count_tried_or_bytes(conn, test_folder: Path):
    (test_folder / "doc.md").write_text("# A real doc with some content\n")

    first = sync_filesystem(conn, str(test_folder))
    assert first["ext_stats"][".md"]["tried"] == 1

    second = sync_filesystem(conn, str(test_folder))
    assert second["fast_skipped"] == 1
    assert ".md" not in second["ext_stats"] or second["ext_stats"][".md"]["tried"] == 0
    assert ".md" not in second["ext_stats"] or second["ext_stats"][".md"]["bytes"] == 0


def test_pptx_and_xlsx_are_still_skipped_by_the_walk(conn, test_folder: Path):
    """.pptx / .xlsx need python-pptx / openpyxl, which are not installed -
    they must not be picked up (and must not crash the sync)."""
    (test_folder / "deck.pptx").write_bytes(b"PK\x03\x04 not really a pptx")
    (test_folder / "sheet.xlsx").write_bytes(b"PK\x03\x04 not really an xlsx")
    (test_folder / "real.md").write_text("# Real\n")

    from jarvis_server.connectors.filesystem import walk_directory
    walked = {p.name for p in walk_directory(test_folder)}
    assert walked == {"real.md"}

    stats = sync_filesystem(conn, str(test_folder))
    assert stats["errors"] == 0
    assert stats["pulled"] == 1


def test_covered_root_ids_returns_subfolders_of_this_root(test_folder: Path):
    connector = FilesystemConnector(str(test_folder))
    sub = str(test_folder / "sub")
    sibling = str(test_folder.parent / f"{test_folder.name}-archive")  # shared prefix, not nested
    unrelated = str(test_folder.parent / "elsewhere")

    covered = connector.covered_root_ids([sub, sibling, unrelated, connector.root_id])

    assert covered == [sub]


def test_covered_root_ids_returns_nothing_for_an_unrelated_or_ancestor_root(test_folder: Path):
    connector = FilesystemConnector(str(test_folder))
    parent = str(test_folder.parent)

    assert connector.covered_root_ids([parent, str(test_folder.parent / "elsewhere")]) == []


def _sync_tree(conn, test_folder: Path) -> tuple[Path, Path]:
    """watched_root/{a.md, sub/b.md} plus a sibling watched_root-archive/c.md
    that shares the name prefix without being nested."""
    sub = test_folder / "sub"
    sub.mkdir()
    sibling = test_folder.parent / f"{test_folder.name}-archive"
    sibling.mkdir()
    (test_folder / "a.md").write_text("# A\n")
    (sub / "b.md").write_text("# B\n")
    (sibling / "c.md").write_text("# C\n")
    sync_filesystem(conn, str(test_folder))
    sync_filesystem(conn, str(sibling))
    return sub, sibling


def test_owned_source_ids_leaves_files_another_tracked_root_still_claims(conn, test_folder: Path):
    sub, sibling = _sync_tree(conn, test_folder)
    connector = FilesystemConnector(str(test_folder))
    a, b = str((test_folder / "a.md").resolve()), str((sub / "b.md").resolve())

    assert connector.owned_source_ids(conn, [str(sibling.resolve())]) == {a, b}
    assert connector.owned_source_ids(conn, [str(sub.resolve()), str(sibling.resolve())]) == {a}


def test_owned_source_ids_of_a_subfolder_is_empty_while_its_parent_is_tracked(conn, test_folder: Path):
    sub, _sibling = _sync_tree(conn, test_folder)

    assert FilesystemConnector(str(sub)).owned_source_ids(conn, [str(test_folder.resolve())]) == set()


def test_remove_source_tombstones_the_root_and_only_its_own_files(conn, test_folder: Path):
    from jarvis_server import pipeline

    _sub, sibling = _sync_tree(conn, test_folder)
    for root in (test_folder, sibling):
        db.record_sync_root(conn, "filesystem", str(root.resolve()), "2026-10-16T00:00:00+00:00")

    removed = pipeline.remove_source(conn, FilesystemConnector(str(test_folder)))

    assert removed == 2
    assert [row["root_id"] for row in db.get_sync_roots(conn)] == [str(sibling.resolve())]
    assert db.get_active_source_ids_for_connector(conn, "filesystem") == {str((sibling / "c.md").resolve())}
    # Tombstoned, never deleted: the rows and the files on disk are all still there.
    assert conn.execute("SELECT COUNT(*) FROM document_sources").fetchone()[0] == 3
    assert (test_folder / "a.md").exists()


def test_syncing_a_removed_root_again_brings_it_back(conn, test_folder: Path):
    from jarvis_server import pipeline

    _sync_tree(conn, test_folder)
    pipeline.remove_source(conn, FilesystemConnector(str(test_folder)))

    sync_filesystem(conn, str(test_folder))

    assert str((test_folder / "a.md").resolve()) in db.get_active_source_ids_for_connector(conn, "filesystem")
