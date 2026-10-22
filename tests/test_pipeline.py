"""End-to-end check for the ingestion pipeline orchestrator
(jarvis/modules/pipeline.py).

Confirms the full chain runs with zero manual bridging: a folder of files
-> run_ingest -> chunks land in the DB -> embed_and_index_chunks
sees total_chunks > 0 -> search() returns real ranked results. Uses a
deterministic stub embedder throughout, so no network / API key is needed.
"""

from pathlib import Path

import pytest

from jarvis_server.internal import db
from jarvis_server.internal.embed.embed import EMBEDDING_DIMENSIONS, Embedder
from jarvis_server.internal.llm.llm import LLMClient
from jarvis_server.internal.migrate import run_migrations
from jarvis_server.connectors.drive import DriveConnector
from jarvis_server.connectors.drive import sync as drive_sync_mod
from jarvis_server.connectors.filesystem import FilesystemConnector
from jarvis_server.mcp_tools import search as search_mod
from jarvis_server.mcp_tools.hierarchy import _get_hierarchy_impl
from jarvis_server.mcp_tools.models import SearchFilters, SearchResult
from jarvis_server.mcp_tools.search import search
from jarvis_server.organize import config
from jarvis_server.internal.embed.embed import IngestCancelled
from jarvis_server.pipeline import (
    PHASE_DONE,
    PHASE_GENERATING_EMBEDDINGS,
    PHASE_ORGANIZING,
    PHASE_PROCESSING_DOCUMENTS,
    PHASE_READING_FILES,
    PHASE_RECONCILING,
    run_ingest,
)


class StubEmbedder(Embedder):
    """Deterministic 1024-dim vector per input text (derived from the text
    itself), so the whole pipeline - including the sqlite-vec KNN and
    RRF fusion - runs offline."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            seed = sum(ord(c) for c in text) or 1
            vectors.append([((seed * (i + 1)) % 97) / 97.0 for i in range(EMBEDDING_DIMENSIONS)])
        return vectors


class TopicEmbedder(Embedder):
    """Puts each chunk on one of a few near-orthogonal axes by keyword, so
    organize() produces real, hand-checkable clusters offline."""

    _KEYWORDS = ["coffee", "sqlite", "photosynthesis", "roman"]

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            v = [0.0] * EMBEDDING_DIMENSIONS
            low = t.lower()
            hit = False
            for i, kw in enumerate(self._KEYWORDS):
                if kw in low:
                    v[i] = 1.0
                    hit = True
            if not hit:
                v[900] = 1.0
            v[500] = (sum(ord(c) for c in t) % 1000) / 1_000_000.0  # unique-ish, tiny
            out.append(v)
        return out


class CyclingLLM(LLMClient):
    """Always returns a valid label; enough for a wiring test."""

    def __init__(self):
        self._i = 0

    def complete(self, system, user, *, temperature=0.2, max_tokens=400):
        self._i += 1
        return f'{{"title": "Topic {self._i}", "description": "desc {self._i}."}}'


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "pipeline.db"
    run_migrations(path)
    return path


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    folder = tmp_path / "corpus"
    folder.mkdir()
    (folder / "greece.md").write_text(
        "# Athens\n\nThe Parthenon sits on the Acropolis of Athens, "
        "completed around 438 BC as a temple to Athena.\n"
    )
    (folder / "whales.txt").write_text(
        "The blue whale is the largest animal known to have existed, "
        "reaching about 30 metres in length.\n"
    )
    (folder / "espresso.md").write_text(
        "# Espresso\n\nA double shot of espresso is pulled from roughly "
        "18 grams of finely ground coffee.\n"
    )
    return folder


def test_full_pipeline_folder_to_search(db_path: Path, corpus: Path, monkeypatch):
    conn = db.get_connection(db_path)
    assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0

    # this test targets the sync->chunk->embed->search sub-path; Phase B
    # (organize+reconcile) has its own end-to-end test below.
    result = run_ingest(
        conn, FilesystemConnector(str(corpus)), embedder=StubEmbedder(), organize_hierarchy=False
    )

    # sync stage
    assert result["sync"]["pulled"] == 3
    assert result["sync"]["errors"] == 0
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 3

    # chunk stage
    n_chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    assert n_chunks >= 3
    assert result["chunks_created"] == n_chunks

    # embed + index stage - the thing the smoke test found was never reached
    assert result["embed"]["total_chunks"] == n_chunks
    assert result["embed"]["total_chunks"] > 0
    assert result["embed"]["newly_embedded"] == n_chunks
    assert conn.execute("SELECT count(*) FROM chunk_vectors").fetchone()[0] == n_chunks
    assert conn.execute("SELECT count(*) FROM chunks_fts").fetchone()[0] == n_chunks

    # search() end to end against the same store, no manual bridging
    real_get_connection = db.get_connection
    monkeypatch.setattr(search_mod.db, "get_connection", lambda: real_get_connection(db_path))
    monkeypatch.setattr(search_mod, "OpenRouterVoyageEmbedder", StubEmbedder)

    res = search("Parthenon Acropolis Athens")
    assert isinstance(res, SearchResult)
    assert len(res.results) > 0
    top = res.results[0]
    assert "Parthenon" in top.text
    assert top.sources[0].title == "greece.md"
    assert top.score > 0

    conn.close()


def test_run_ingest_persists_file_type_stats(db_path: Path, corpus: Path):
    """`ext_stats` from sync_filesystem must land in file_type_stats via
    upsert_file_type_stats, and must not leak into the returned sync dict
    that other consumers (on_progress, _check_cancel) rely on."""
    conn = db.get_connection(db_path)

    result = run_ingest(
        conn, FilesystemConnector(str(corpus)), embedder=StubEmbedder(), organize_hierarchy=False
    )

    assert "ext_stats" not in result["sync"]

    rows = {row["extension"]: row for row in db.get_file_type_stats(conn)}
    assert rows[".md"]["tried_count"] == 2  # greece.md, espresso.md
    assert rows[".md"]["bytes_processed"] > 0
    assert rows[".txt"]["tried_count"] == 1  # whales.txt
    assert rows[".txt"]["bytes_processed"] > 0

    conn.close()


def test_run_ingest_records_sync_root_on_clean_sync(db_path: Path, corpus: Path):
    conn = db.get_connection(db_path)
    connector = FilesystemConnector(str(corpus))

    run_ingest(conn, connector, embedder=StubEmbedder(), organize_hierarchy=False)

    rows = db.get_sync_roots(conn)
    assert len(rows) == 1
    assert rows[0]["connector"] == "filesystem"
    assert rows[0]["root_id"] == connector.root_id
    assert rows[0]["first_synced_at"] == rows[0]["last_synced_at"]

    conn.close()


def test_run_ingest_tombstones_a_subfolder_root_once_its_parent_is_synced(db_path: Path, corpus: Path):
    conn = db.get_connection(db_path)
    sub = corpus / "sub"
    sub.mkdir()
    (sub / "note.md").write_text("# Note\n\nSub-folder content.\n")

    sub_connector = FilesystemConnector(str(sub))
    run_ingest(conn, sub_connector, embedder=StubEmbedder(), organize_hierarchy=False)
    assert [row["root_id"] for row in db.get_sync_roots(conn)] == [sub_connector.root_id]

    parent_connector = FilesystemConnector(str(corpus))
    run_ingest(conn, parent_connector, embedder=StubEmbedder(), organize_hierarchy=False)

    roots = [row["root_id"] for row in db.get_sync_roots(conn)]
    assert roots == [parent_connector.root_id]  # subfolder's row is superseded

    conn.close()


def test_pipeline_rerun_is_idempotent(db_path: Path, corpus: Path):
    conn = db.get_connection(db_path)

    first = run_ingest(
        conn, FilesystemConnector(str(corpus)), embedder=StubEmbedder(), organize_hierarchy=False
    )
    assert first["chunks_created"] > 0

    second = run_ingest(
        conn, FilesystemConnector(str(corpus)), embedder=StubEmbedder(), organize_hierarchy=False
    )
    assert second["chunks_created"] == 0
    assert second["embed"]["newly_embedded"] == 0
    assert second["embed"]["api_calls_made"] == 0

    # nothing duplicated
    assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == first["chunks_created"]
    assert (
        conn.execute("SELECT count(*) FROM chunk_vectors").fetchone()[0]
        == first["chunks_created"]
    )

    conn.close()


def test_csv_and_html_files_ingest_and_are_searchable(db_path: Path, tmp_path: Path, monkeypatch):
    """A folder mixing .md / .csv / .html goes all the way through
    run_ingest and every file's content is retrievable."""
    folder = tmp_path / "mixed_types"
    folder.mkdir()
    (folder / "readme.md").write_text("# Onboarding\n\nStart with the runbook.\n")
    (folder / "contacts.csv").write_text(
        "name,team\nPriya Nair,platform\nMarco Rossi,billing\n"
    )
    (folder / "release.html").write_text(
        "<h1>Release 2.4</h1><p>The <b>quokka</b> migration completed.</p>"
    )

    conn = db.get_connection(db_path)
    result = run_ingest(
        conn, FilesystemConnector(str(folder)), embedder=StubEmbedder(), organize_hierarchy=False
    )
    assert result["sync"]["pulled"] == 3
    assert result["sync"]["errors"] == 0
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 3

    real_get_connection = db.get_connection
    monkeypatch.setattr(search_mod.db, "get_connection", lambda: real_get_connection(db_path))
    monkeypatch.setattr(search_mod, "OpenRouterVoyageEmbedder", StubEmbedder)

    assert "contacts.csv" in {s.title for r in search("Priya Nair platform").results for s in r.sources}
    assert "release.html" in {s.title for r in search("quokka migration completed").results for s in r.sources}

    conn.close()


def test_file_type_filter_works_end_to_end_on_real_mime(db_path: Path, tmp_path: Path, monkeypatch):
    """issue #51: the connector now writes document_sources.mime on real
    ingestion, so search()'s file_type filter actually narrows results
    instead of matching a NULL column."""
    folder = tmp_path / "mixed"
    folder.mkdir()
    (folder / "handbook.md").write_text("# Handbook\n\nonboarding checklist and policy links\n")
    (folder / "changelog.txt").write_text("onboarding checklist moved to the wiki\n")

    conn = db.get_connection(db_path)
    run_ingest(conn, FilesystemConnector(str(folder)), embedder=StubEmbedder(), organize_hierarchy=False)

    mime_by_name = {
        Path(r["source_id"]).name: r["mime"]
        for r in conn.execute("SELECT source_id, mime FROM document_sources")
    }
    assert mime_by_name == {"handbook.md": "text/markdown", "changelog.txt": "text/plain"}

    real_get_connection = db.get_connection
    monkeypatch.setattr(search_mod.db, "get_connection", lambda: real_get_connection(db_path))
    monkeypatch.setattr(search_mod, "OpenRouterVoyageEmbedder", StubEmbedder)

    unfiltered = search("onboarding checklist")
    assert {s.title for r in unfiltered.results for s in r.sources} == {"handbook.md", "changelog.txt"}

    md_only = search("onboarding checklist", filters=SearchFilters(file_type="text/markdown"))
    assert md_only.results
    assert {s.title for r in md_only.results for s in r.sources} == {"handbook.md"}

    conn.close()


def test_ingesting_a_second_folder_keeps_the_first_searchable(db_path: Path, tmp_path: Path, monkeypatch):
    """issue #61 repro, end to end: ingest folder A, then folder B (same
    connector, no override), and folder A's content is still searchable."""
    folder_a = tmp_path / "team_a"
    folder_a.mkdir()
    (folder_a / "aardvark.md").write_text("# Aardvark\n\nThe aardvark forages for termites at night.\n")
    (folder_a / "antarctica.md").write_text("# Antarctica\n\nAntarctica is the coldest continent.\n")
    folder_b = tmp_path / "team_b"
    folder_b.mkdir()
    (folder_b / "gorilla.md").write_text("# Gorilla\n\nGorillas live in troops led by a silverback.\n")

    conn = db.get_connection(db_path)
    run_ingest(conn, FilesystemConnector(str(folder_a)), embedder=StubEmbedder(), organize_hierarchy=False)
    r_b = run_ingest(conn, FilesystemConnector(str(folder_b)), embedder=StubEmbedder(), organize_hierarchy=False)
    assert r_b["sync"]["tombstoned"] == 0

    live = {
        Path(r["source_id"]).name
        for r in conn.execute("SELECT source_id FROM document_sources WHERE tombstoned = 0")
    }
    assert live == {"aardvark.md", "antarctica.md", "gorilla.md"}

    real_get_connection = db.get_connection
    monkeypatch.setattr(search_mod.db, "get_connection", lambda: real_get_connection(db_path))
    monkeypatch.setattr(search_mod, "OpenRouterVoyageEmbedder", StubEmbedder)

    hits = {s.title for r in search("aardvark termites forages").results for s in r.sources}
    assert "aardvark.md" in hits           # folder A content, ingested first, still searchable

    conn.close()


# ---------------------------------------------------------------------
# Phase B: organize + reconcile wired into the pipeline
# ---------------------------------------------------------------------

@pytest.fixture
def topical_corpus(tmp_path: Path) -> Path:
    folder = tmp_path / "topical"
    folder.mkdir()
    (folder / "coffee_a.md").write_text(
        "# Espresso\n\nEspresso forces hot water through ground coffee under pressure; "
        "coffee extraction yield sets the flavour balance.\n"
    )
    (folder / "coffee_b.md").write_text(
        "# Roasting\n\nRoasting coffee beans drives Maillard reactions; a darker coffee "
        "roast tastes more bitter than a light coffee roast.\n"
    )
    (folder / "sqlite_a.md").write_text(
        "# SQLite WAL\n\nSQLite write-ahead logging lets one SQLite writer and many "
        "SQLite readers proceed without blocking.\n"
    )
    (folder / "sqlite_b.md").write_text(
        "# SQLite Indexes\n\nA SQLite B-tree index lets the SQLite planner skip a full "
        "table scan at the cost of extra SQLite write work.\n"
    )
    (folder / "bio_a.md").write_text(
        "# Photosynthesis\n\nPhotosynthesis turns carbon dioxide and water into glucose "
        "using light; photosynthesis releases oxygen.\n"
    )
    (folder / "bio_b.md").write_text(
        "# Light Reactions\n\nThe photosynthesis light reactions in chlorophyll drive "
        "electron transport; photosynthesis stores energy as ATP.\n"
    )
    return folder


# ---------------------------------------------------------------------
# on_phase callback (docs/desktop-app-ux-redesign-DESIGN.md §4c) - the
# daemon's live ingest-status polling is built on this; this file
# checks the callback contract directly, without any daemon/HTTP
# involved, since that's what run_ingest itself actually guarantees.
# ---------------------------------------------------------------------

def test_on_phase_reports_every_stage_in_order_when_organize_runs(
    db_path: Path, topical_corpus: Path
):
    conn = db.get_connection(db_path)
    graph_path = db_path.parent / "on_phase_graph.lbdb"
    phases = []

    result = run_ingest(
        conn, FilesystemConnector(str(topical_corpus)),
        embedder=TopicEmbedder(), llm_client=CyclingLLM(), live_graph_path=graph_path,
        on_phase=phases.append,
    )

    assert "organize" in result["hierarchy"]  # confirms organize/reconcile really ran
    assert phases == [
        PHASE_READING_FILES,
        PHASE_PROCESSING_DOCUMENTS,
        PHASE_GENERATING_EMBEDDINGS,
        PHASE_ORGANIZING,
        PHASE_RECONCILING,
        PHASE_DONE,
    ]

    conn.close()


def test_on_phase_skips_organize_and_reconcile_when_organize_hierarchy_is_false(
    db_path: Path, corpus: Path
):
    conn = db.get_connection(db_path)
    phases = []

    run_ingest(
        conn, FilesystemConnector(str(corpus)), embedder=StubEmbedder(),
        organize_hierarchy=False, on_phase=phases.append,
    )

    assert phases == [
        PHASE_READING_FILES,
        PHASE_PROCESSING_DOCUMENTS,
        PHASE_GENERATING_EMBEDDINGS,
        PHASE_DONE,
    ]

    conn.close()


def test_on_phase_is_never_called_when_not_given(db_path: Path, corpus: Path):
    """The default, every existing call site's behavior - on_phase must
    stay strictly opt-in, not something a caller pays for just by
    calling run_ingest() at all."""
    conn = db.get_connection(db_path)

    result = run_ingest(
        conn, FilesystemConnector(str(corpus)), embedder=StubEmbedder(), organize_hierarchy=False,
    )

    assert result["chunks_created"] > 0
    conn.close()


def test_on_batch_progress_is_forwarded_to_embed_and_index_chunks(db_path: Path, corpus: Path):
    """run_ingest's on_batch_progress is a plain pass-through to
    embed_and_index_chunks's on_batch (see embed.py's own tests for the
    callback's exact call shape) - this only checks the wiring."""
    conn = db.get_connection(db_path)
    calls = []

    result = run_ingest(
        conn, FilesystemConnector(str(corpus)), embedder=StubEmbedder(),
        organize_hierarchy=False, on_batch_progress=lambda *args: calls.append(args),
    )

    assert result["chunks_created"] > 0
    assert calls  # at least the initial (0, total_batches, 0, total_items) call
    final_batch, total_batches, processed, total = calls[-1]
    assert final_batch == total_batches
    assert processed == total

    conn.close()


def test_on_progress_reports_item_counts_tagged_by_the_phase_on_phase_last_announced(
    db_path: Path, corpus: Path,
):
    conn = db.get_connection(db_path)
    events = []

    run_ingest(
        conn, FilesystemConnector(str(corpus)), embedder=StubEmbedder(), organize_hierarchy=False,
        on_phase=lambda phase: events.append(("phase", phase)),
        on_progress=lambda *counts: events.append(("progress", counts)),
    )

    last_progress_by_phase = {}
    current_phase = None
    for kind, value in events:
        if kind == "phase":
            current_phase = value
        else:
            last_progress_by_phase[current_phase] = value
    file_count = len(list(corpus.iterdir()))
    assert last_progress_by_phase[PHASE_READING_FILES] == (file_count, file_count, 0)
    processed_docs, total_docs, _ = last_progress_by_phase[PHASE_PROCESSING_DOCUMENTS]
    assert processed_docs == total_docs > 0
    processed_chunks, total_chunks, _ = last_progress_by_phase[PHASE_GENERATING_EMBEDDINGS]
    assert processed_chunks == total_chunks > 0

    conn.close()


# ---------------------------------------------------------------------
# ingest_runs / ingest_run_phases (migration 0016) + cancellation
# ---------------------------------------------------------------------

def test_run_ingest_records_a_done_run_and_every_phase_it_reached(db_path: Path, corpus: Path):
    conn = db.get_connection(db_path)

    result = run_ingest(
        conn, FilesystemConnector(str(corpus)), embedder=StubEmbedder(),
        organize_hierarchy=False, trigger="gui",
    )

    run_row = db.get_ingest_run(conn, result["run_id"])
    assert run_row["status"] == db.RUN_STATUS_DONE
    assert run_row["connector"] == "filesystem"
    assert run_row["trigger"] == "gui"
    assert run_row["ended_at"] is not None

    phases = {row["phase"]: row["status"] for row in db.get_ingest_run_phases(conn, result["run_id"])}
    assert phases == {
        PHASE_READING_FILES: db.PHASE_STATUS_DONE,
        PHASE_PROCESSING_DOCUMENTS: db.PHASE_STATUS_DONE,
        PHASE_GENERATING_EMBEDDINGS: db.PHASE_STATUS_DONE,
    }  # organize/reconcile skipped (organize_hierarchy=False) - no rows for them

    conn.close()


def test_run_ingest_marks_the_run_failed_when_a_stage_raises(db_path: Path, tmp_path: Path):
    """A dimension-mismatch embedder failure (embed.py's own test already
    covers the mechanics) must also flip the run's own status - not just
    the phase's."""
    conn = db.get_connection(db_path)
    (tmp_path / "f.txt").write_text("some text")

    class WrongDimensionEmbedder(Embedder):
        batch_size = 100

        def embed(self, texts):
            return [[0.0] * 512 for _ in texts]

    with pytest.raises(ValueError, match="embedding dimension mismatch"):
        run_ingest(
            conn, FilesystemConnector(str(tmp_path)), embedder=WrongDimensionEmbedder(),
            organize_hierarchy=False,
        )

    run_row = db.get_recent_ingest_runs(conn)[0]
    assert run_row["status"] == db.RUN_STATUS_FAILED

    conn.close()


def test_run_ingest_marks_the_run_cancelled_and_stops_before_the_next_phase(
    db_path: Path, corpus: Path
):
    conn = db.get_connection(db_path)

    with pytest.raises(IngestCancelled):
        run_ingest(
            conn, FilesystemConnector(str(corpus)), embedder=StubEmbedder(),
            organize_hierarchy=False, on_cancel_requested=lambda: True,
        )

    run_row = db.get_recent_ingest_runs(conn)[0]
    assert run_row["status"] == db.RUN_STATUS_CANCELLED

    phases = db.get_ingest_run_phases(conn, run_row["id"])
    # cancel is checked right after sync finishes, before chunk begins -
    # sync is the only phase that got a row at all.
    assert [row["phase"] for row in phases] == [PHASE_READING_FILES]
    assert phases[0]["status"] == db.PHASE_STATUS_DONE  # the phase itself still finished cleanly

    conn.close()


def test_a_cancel_during_organize_stops_after_the_cluster_in_flight_and_the_next_run_resumes(
    db_path: Path, topical_corpus: Path
):
    conn = db.get_connection(db_path)
    graph_path = db_path.parent / "cancel_organize_graph.lbdb"
    llm = CyclingLLM()
    state = {"phase": None, "labelled": 0}

    def _on_progress(processed, total, errors):
        if state["phase"] == PHASE_ORGANIZING:
            state["labelled"] = processed

    with pytest.raises(IngestCancelled):
        run_ingest(
            conn, FilesystemConnector(str(topical_corpus)),
            embedder=TopicEmbedder(), llm_client=llm, live_graph_path=graph_path,
            on_phase=lambda phase: state.update(phase=phase),
            on_progress=_on_progress,
            on_cancel_requested=lambda: state["labelled"] >= 1,
        )

    assert state["labelled"] == 1  # stopped after the first cluster, not the whole phase
    assert db.get_recent_ingest_runs(conn)[0]["status"] == db.RUN_STATUS_CANCELLED
    assert db.get_in_progress_organize_run(conn) is not None  # left resumable

    result = run_ingest(
        conn, FilesystemConnector(str(topical_corpus)),
        embedder=TopicEmbedder(), llm_client=llm, live_graph_path=graph_path,
    )
    assert "organize" in result["hierarchy"]

    conn.close()


def test_phase_b_builds_a_hierarchy_and_a_second_run_is_a_no_op(
    db_path: Path, topical_corpus: Path
):
    conn = db.get_connection(db_path)
    graph_path = db_path.parent / "pipeline_graph.lbdb"
    phases = []

    first = run_ingest(
        conn, FilesystemConnector(str(topical_corpus)),
        embedder=TopicEmbedder(), llm_client=CyclingLLM(), live_graph_path=graph_path,
        on_phase=phases.append,
    )

    # Phase B ran - and on_phase saw every stage, in order, including the
    # two only a real hierarchy rebuild reaches.
    assert phases == [
        PHASE_READING_FILES,
        PHASE_PROCESSING_DOCUMENTS,
        PHASE_GENERATING_EMBEDDINGS,
        PHASE_ORGANIZING,
        PHASE_RECONCILING,
        PHASE_DONE,
    ]
    assert "organize" in first["hierarchy"] and "reconcile" in first["hierarchy"]
    assert first["hierarchy"]["reconcile"]["born"] >= 3          # coffee / sqlite / bio
    assert graph_path.exists()

    # persisted: real topic rows + the reserved root + Unfiled, an event log,
    # every chunk placed, cache synced
    n_chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    placed = conn.execute(
        "SELECT count(*) FROM chunks WHERE topic_id IS NOT NULL"
    ).fetchone()[0]
    assert placed == n_chunks
    born_events = conn.execute(
        "SELECT count(*) FROM topic_events WHERE event_type = 'born'"
    ).fetchone()[0]
    assert born_events == first["hierarchy"]["reconcile"]["born"]
    root = conn.execute(
        "SELECT id FROM topics WHERE id = ?", (config.ROOT_TOPIC_ID,)
    ).fetchone()
    assert root is not None

    events_after_first = conn.execute("SELECT count(*) FROM topic_events").fetchone()[0]

    # --- second run, identical folder: no new chunks, no new topic events ---
    second = run_ingest(
        conn, FilesystemConnector(str(topical_corpus)),
        embedder=TopicEmbedder(), llm_client=CyclingLLM(), live_graph_path=graph_path,
    )

    assert second["chunks_created"] == 0
    assert second["hierarchy"] == {"skipped": "live chunk set unchanged (no-op re-sync)"}
    assert conn.execute("SELECT count(*) FROM topic_events").fetchone()[0] == events_after_first

    conn.close()


def test_phase_b_reflects_a_removed_source_on_the_next_run(
    db_path: Path, topical_corpus: Path
):
    conn = db.get_connection(db_path)
    graph_path = db_path.parent / "pipeline_graph2.lbdb"

    run_ingest(
        conn, FilesystemConnector(str(topical_corpus)),
        embedder=TopicEmbedder(), llm_client=CyclingLLM(), live_graph_path=graph_path,
    )
    events_after_first = conn.execute("SELECT count(*) FROM topic_events").fetchone()[0]

    # delete both coffee files, re-run
    (topical_corpus / "coffee_a.md").unlink()
    (topical_corpus / "coffee_b.md").unlink()

    second = run_ingest(
        conn, FilesystemConnector(str(topical_corpus)),
        embedder=TopicEmbedder(), llm_client=CyclingLLM(), live_graph_path=graph_path,
    )

    # the live chunk set shrank -> Phase B ran again and recorded events
    assert "organize" in second["hierarchy"]
    assert conn.execute("SELECT count(*) FROM topic_events").fetchone()[0] > events_after_first
    # the removed chunks are no longer placed anywhere live
    coffee_chunks = conn.execute(
        "SELECT c.chunk_id FROM chunks c JOIN documents d ON c.content_hash = d.content_hash "
        "JOIN document_sources s ON d.content_hash = s.content_hash "
        "WHERE s.source_id LIKE '%coffee%' AND s.tombstoned = 0"
    ).fetchall()
    assert coffee_chunks == []

    conn.close()


# ---------------------------------------------------------------------
# DriveConnector: the Drive pull stage wired into the same shared
# chunk/embed/organize path as FilesystemConnector, via run_ingest.
# ---------------------------------------------------------------------

def _fake_drive_service(docs: dict[str, bytes]):
    """A stub Drive v3 service pre-loaded with Google Docs, using the same
    fake as test_drive_connector."""
    from tests.test_drive_connector import GDOC_MIME, _FakeDriveService

    listing = [
        {"id": fid, "name": fid, "mimeType": GDOC_MIME,
         "modifiedTime": "2026-08-01T00:00:00.000Z"}
        for fid in docs
    ]
    return _FakeDriveService(content=dict(docs), listing=listing)


def test_drive_connector_without_folder_id_raises_clearly():
    """No whole-Drive mode: a live test against a real account found the
    previously-unscoped listing pulls the ENTIRE accessible Drive (1,567
    files, 910 downloadable) with no way to opt out. folder_id is required -
    a missing/empty one must raise before anything is fetched, not silently
    list everything. Validated eagerly in __init__, so this needs no conn,
    no service, and never reaches sync()."""
    with pytest.raises(ValueError, match="folder_id"):
        DriveConnector(folder_id=None)


def test_drive_connector_threads_folder_id_into_sync_drive(db_path: Path, monkeypatch):
    """Closes the exact gap found during live testing: DriveConnector's own
    folder_id must actually reach sync_drive, not just be accepted and
    dropped. (sync_drive's own test - test_drive_connector.py - separately
    confirms folder_id reaches the real files().list() query.)"""
    conn = db.get_connection(db_path)
    calls = []

    def _spy_sync_drive(conn_arg, service_arg, folder_id=None, connector="drive", on_progress=None):
        calls.append({"folder_id": folder_id, "connector": connector})
        return {"pulled": 0, "fast_skipped": 0, "skipped": 0, "tombstoned": 0, "errors": 0}

    # Patched on the sync submodule, not the drive package: DriveConnector
    # resolves sync_drive as a bare name from within that module.
    monkeypatch.setattr(drive_sync_mod, "sync_drive", _spy_sync_drive)

    connector = DriveConnector("real-folder-abc123", service=object())
    run_ingest(conn, connector, embedder=StubEmbedder(), organize_hierarchy=False)

    assert calls == [{"folder_id": "real-folder-abc123", "connector": "drive"}]
    conn.close()


def test_drive_connector_wires_sync_drive_into_chunk_and_embed(db_path: Path):
    """Wiring only: DriveConnector.sync calls sync_drive, then the SAME
    chunk + embed stages FilesystemConnector uses - not re-testing their
    internals."""
    conn = db.get_connection(db_path)
    service = _fake_drive_service({
        "g1": b"# Espresso\n\nA double shot of espresso uses about 18 grams of ground coffee.\n",
        "g2": b"# SQLite WAL\n\nWrite-ahead logging lets one writer and many readers proceed.\n",
    })

    result = run_ingest(
        conn, DriveConnector("folder-under-test", service=service),
        embedder=StubEmbedder(), organize_hierarchy=False,
    )

    # Drive pull stats shape (no "renamed" - Drive fileId is stable identity)
    assert set(result["sync"]) == {"pulled", "fast_skipped", "skipped", "tombstoned", "errors"}
    assert result["sync"]["pulled"] == 2
    assert result["sync"]["errors"] == 0
    assert conn.execute(
        "SELECT count(*) FROM document_sources WHERE connector = 'drive'"
    ).fetchone()[0] == 2

    # ... then the shared downstream stages ran
    n_chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    assert n_chunks >= 2
    assert result["chunks_created"] == n_chunks
    assert result["embed"]["total_chunks"] == n_chunks
    assert result["embed"]["newly_embedded"] == n_chunks
    assert conn.execute("SELECT count(*) FROM chunk_vectors").fetchone()[0] == n_chunks
    assert conn.execute("SELECT count(*) FROM chunks_fts").fetchone()[0] == n_chunks
    assert result["hierarchy"] == {"skipped": "organize_hierarchy=False"}

    conn.close()


def test_drive_connector_reaches_phase_b(db_path: Path):
    """Wiring: with organize_hierarchy=True the same _hierarchy_rebuild_reason
    / _run_organize / _run_reconcile path FilesystemConnector uses fires for
    a Drive pull."""
    conn = db.get_connection(db_path)
    graph_path = db_path.parent / "drive_pipeline_graph.lbdb"
    service = _fake_drive_service({
        "c1": b"# Espresso\n\nEspresso forces hot water through ground coffee under pressure.\n",
        "c2": b"# Roasting\n\nRoasting coffee beans drives Maillard reactions in the coffee.\n",
        "s1": b"# SQLite WAL\n\nSQLite write-ahead logging lets one SQLite writer proceed.\n",
        "s2": b"# SQLite Index\n\nA SQLite B-tree index helps the SQLite planner skip a scan.\n",
        "p1": b"# Photosynthesis\n\nPhotosynthesis turns carbon dioxide and water into glucose.\n",
        "p2": b"# Light Reactions\n\nThe photosynthesis light reactions drive electron transport.\n",
    })

    result = run_ingest(
        conn, DriveConnector("folder-under-test", service=service),
        embedder=TopicEmbedder(), llm_client=CyclingLLM(), live_graph_path=graph_path,
    )

    assert result["sync"]["pulled"] == 6
    assert "organize" in result["hierarchy"] and "reconcile" in result["hierarchy"]
    assert result["hierarchy"]["reconcile"]["born"] >= 3          # coffee / sqlite / bio
    assert graph_path.exists()
    n_chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    placed = conn.execute(
        "SELECT count(*) FROM chunks WHERE topic_id IS NOT NULL"
    ).fetchone()[0]
    assert placed == n_chunks

    conn.close()


# ---------------------------------------------------------------------
# low_confidence reaches get_hierarchy() via organize_state (migration 0011)
# see docs/LOW_CONFIDENCE_DESIGN.md
# ---------------------------------------------------------------------

@pytest.fixture
def wide_corpus(tmp_path: Path) -> Path:
    """>= 15 tiny files -> >= 15 embedded chunks, spread over the four
    TopicEmbedder keyword axes so organize() has real structure to find."""
    folder = tmp_path / "wide"
    folder.mkdir()
    topics = {
        "coffee": "Espresso pulls coffee under pressure; coffee roast sets the flavour.",
        "sqlite": "SQLite write-ahead logging lets one SQLite writer and many readers proceed.",
        "photosynthesis": "Photosynthesis turns carbon dioxide and water into glucose using light.",
        "roman": "The Roman legion was the backbone of the Roman army for centuries.",
    }
    n = 0
    for kw, sentence in topics.items():
        for i in range(4):
            (folder / f"{kw}_{i}.md").write_text(f"# {kw.title()} {i}\n\n{sentence} Note {i}.\n")
            n += 1
    assert n >= 15
    return folder


def test_small_corpus_run_surfaces_low_confidence_true_on_get_hierarchy(
    db_path: Path, topical_corpus: Path
):
    conn = db.get_connection(db_path)
    graph_path = db_path.parent / "small_lc_graph.lbdb"

    result = run_ingest(
        conn, FilesystemConnector(str(topical_corpus)),
        embedder=TopicEmbedder(), llm_client=CyclingLLM(), live_graph_path=graph_path,
    )
    assert "organize" in result["hierarchy"]                     # Phase B actually ran
    n_chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    assert n_chunks < 15

    low_confidence, reason = db.get_organize_state(conn)
    assert low_confidence is True
    assert reason is not None and "provisional" in reason

    hierarchy = _get_hierarchy_impl(conn, None, None)
    assert hierarchy.low_confidence is True
    assert hierarchy.low_confidence_reason == reason
    assert hierarchy.low_confidence_reason.startswith("only ")
    assert "embedded chunk(s)" in hierarchy.low_confidence_reason

    conn.close()


def test_large_corpus_run_leaves_low_confidence_false_on_get_hierarchy(
    db_path: Path, wide_corpus: Path
):
    conn = db.get_connection(db_path)
    graph_path = db_path.parent / "wide_lc_graph.lbdb"

    result = run_ingest(
        conn, FilesystemConnector(str(wide_corpus)),
        embedder=TopicEmbedder(), llm_client=CyclingLLM(), live_graph_path=graph_path,
    )
    assert "organize" in result["hierarchy"]
    assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] >= 15

    assert db.get_organize_state(conn) == (False, None)

    hierarchy = _get_hierarchy_impl(conn, None, None)
    assert hierarchy.low_confidence is False
    assert hierarchy.low_confidence_reason is None

    conn.close()


def test_no_op_resync_leaves_organize_state_untouched(
    db_path: Path, topical_corpus: Path
):
    conn = db.get_connection(db_path)
    graph_path = db_path.parent / "noop_lc_graph.lbdb"

    run_ingest(
        conn, FilesystemConnector(str(topical_corpus)),
        embedder=TopicEmbedder(), llm_client=CyclingLLM(), live_graph_path=graph_path,
    )
    assert db.get_organize_state(conn)[0] is True

    # stomp it with a distinctive sentinel, then re-run the identical folder:
    # Phase B is skipped (no-op re-sync), so nothing rewrites the row.
    db.set_organize_state(conn, True, "SENTINEL - must survive a no-op re-sync", "2026-09-03T09:00:00+00:00")

    second = run_ingest(
        conn, FilesystemConnector(str(topical_corpus)),
        embedder=TopicEmbedder(), llm_client=CyclingLLM(), live_graph_path=graph_path,
    )
    assert second["hierarchy"] == {"skipped": "live chunk set unchanged (no-op re-sync)"}
    assert db.get_organize_state(conn) == (True, "SENTINEL - must survive a no-op re-sync")
    assert _get_hierarchy_impl(conn, None, None).low_confidence_reason == (
        "SENTINEL - must survive a no-op re-sync"
    )

    conn.close()
