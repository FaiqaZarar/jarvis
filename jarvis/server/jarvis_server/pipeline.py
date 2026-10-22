"""Ingestion pipeline orchestrator.

Runs pull + normalize + dedup -> chunk -> embed + index -> organize +
reconcile as one continuous operation, for any connector satisfying
`connectors.base.Connector`. Each stage was built and unit-tested in
isolation; this module is the wiring - `run_ingest` is connector-agnostic,
so a new connector (filesystem, Drive, and eventually Notion) plugs in
without this file changing.

The organize+reconcile step finalizes only on a fully successful pull,
and only if the live chunk set actually changed - a true no-op re-sync
skips it entirely.

`on_phase` (docs/desktop-app-ux-redesign-DESIGN.md §4c): an optional
callback invoked with a human-readable phase string at each stage
boundary below. This module has no idea who's listening - the daemon
is the only current caller that passes one, writing each phase into a
module-level var a new `GET /ingest/status` route reads, so a client
that can't get intermediate progress any other way (there's no
streaming response, one HTTP call runs the whole pipeline) can at least
poll for what's happening right now. Kept as a plain callback, not
some persisted/queryable state, so a caller with no interest in
progress (every existing test, the CLI entrypoints) pays nothing for it.

`on_batch_progress`: the same seam, one level more granular, forwarded
straight through to `embed_and_index_chunks`'s own `on_batch` - see its
docstring. Separate from `on_phase` rather than folded into it because
it fires many times within a single phase (once per embedding batch)
with different args (counts, not a phase string).

`on_progress(processed, total, errors)`: item counts for whichever phase
`on_phase` last announced - files for sync, documents for chunk, chunks
for embed, base clusters labelled for organize. `total` is None when the
phase can't know it up front (Drive's incremental sync); `errors` is only
ever non-zero for sync. Reconcile reports none - it's one atomic swap.

`ingest_runs`/`ingest_run_phases` (migration 0016): every call mints a
`run_id` and records it, and each phase it actually reaches, to those
tables - independent of `on_phase`/`on_batch_progress` above, which
exist purely for a live poller and are never read back. A phase this
call skips (organize/reconcile on a no-op re-sync) simply gets no row -
see the migration header. `on_cancel_requested`, checked at each
between-phase checkpoint (and forwarded into `embed_and_index_chunks`
for its own between-batch one, and checked after each labelled cluster
inside organize), raises `IngestCancelled` - the run's
`ingest_runs` row lands as `cancelled` rather than `failed` when that's
what stopped it.
"""

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from dotenv import load_dotenv
from opentelemetry.trace import Span, Status, StatusCode

from jarvis_server.internal.chunk.chunk import chunk_document
from jarvis_server.internal import db, telemetry
from jarvis_server.internal.embed.embed import Embedder, IngestCancelled, embed_and_index_chunks
from jarvis_server.internal.embed.provider import OpenRouterVoyageEmbedder
from jarvis_server.internal.errors import classify
from jarvis_server.internal.llm.llm import LLMClient, LoggingLLMClient, OpenRouterLLM
from jarvis_server.internal.logging.logging_config import get_logger
from jarvis_server.connectors.base import Connector
from jarvis_server.organize import clustering, config_versions, reconcile

# Loaded here, once, deliberately - not in every module that reads env
# vars. Each future entrypoint is responsible for its own load_dotenv()
# call; don't copy this line elsewhere pre-emptively.
load_dotenv()

logger = get_logger(__name__)

# Phase labels for `on_phase` - named constants rather than string
# literals scattered at each call site below, so a test (or a future
# second caller) can assert against `pipeline.PHASE_*` instead of
# duplicating the exact text.
PHASE_READING_FILES = "Reading files"
PHASE_PROCESSING_DOCUMENTS = "Processing documents"
PHASE_GENERATING_EMBEDDINGS = "Generating embeddings"
PHASE_ORGANIZING = "Organizing into topics"
PHASE_RECONCILING = "Reconciling"
PHASE_DONE = "Done"


ProgressCallback = Callable[[int, Optional[int], int], None]

# Stable identifiers for telemetry - the labels above are UI copy and may change.
PHASE_IDS = {
    PHASE_READING_FILES: "reading_files",
    PHASE_PROCESSING_DOCUMENTS: "processing_documents",
    PHASE_GENERATING_EMBEDDINGS: "generating_embeddings",
    PHASE_ORGANIZING: "organizing",
    PHASE_RECONCILING: "reconciling",
}

_tracer = telemetry.get_tracer(__name__)
# Phases are begun and ended from different call sites, so their spans
# can't be `with` blocks; keyed by run_id since that's what both ends share.
_open_phase_spans: dict[str, tuple[str, Span]] = {}


def _set_phase(on_phase: Optional[Callable[[str], None]], phase: str) -> None:
    if on_phase is not None:
        on_phase(phase)


def _begin_phase(
    conn: sqlite3.Connection, run_id: str, phase: str, on_phase: Optional[Callable[[str], None]],
) -> None:
    db.insert_ingest_run_phase(conn, run_id, phase, datetime.now(timezone.utc).isoformat())
    _open_phase_spans[run_id] = (PHASE_IDS[phase], _tracer.start_span(f"ingest.{PHASE_IDS[phase]}"))
    _set_phase(on_phase, phase)


def _end_phase(
    conn: sqlite3.Connection,
    run_id: str,
    phase: str,
    *,
    total_items: Optional[int] = None,
    processed_items: Optional[int] = None,
) -> None:
    db.finish_ingest_run_phase(
        conn, run_id, phase, db.PHASE_STATUS_DONE, datetime.now(timezone.utc).isoformat(),
        total_items=total_items, processed_items=processed_items,
    )
    _, span = _open_phase_spans.pop(run_id, (None, None))
    if span is not None:
        if total_items is not None:
            span.set_attribute("jarvis.items.total", total_items)
        if processed_items is not None:
            span.set_attribute("jarvis.items.processed", processed_items)
        span.end()


def _end_trace_on_failure(
    root: Span, run_id: str, outcome: str, exc: Exception, error_code: Optional[str] = None,
) -> None:
    """Ends the phase span that was running when the ingest stopped, so the
    trace shows which step failed. Only the exception's type is recorded;
    its message can contain file paths."""
    _, phase_span = _open_phase_spans.pop(run_id, (None, None))
    for span in filter(None, (phase_span, root)):
        span.set_attribute("jarvis.outcome", outcome)
        if outcome == "failed":
            span.set_attribute("jarvis.error_type", type(exc).__name__)
            span.set_attribute("jarvis.error_code", error_code)
            span.set_status(Status(StatusCode.ERROR))
    if phase_span is not None:
        phase_span.end()


def _check_cancel(on_cancel_requested: Optional[Callable[[], bool]], partial: dict) -> None:
    if on_cancel_requested is not None and on_cancel_requested():
        raise IngestCancelled(partial)


def _chunk_unchunked_documents(
    conn: sqlite3.Connection, on_progress: Optional[ProgressCallback] = None,
) -> int:
    """Chunk every document that has no rows in `chunks` yet, returning the
    number of chunks created.

    Idempotent by design: a document already represented in `chunks` is
    skipped entirely, so re-running the pipeline over an unchanged corpus
    creates nothing. This mirrors the connector's content-hash dedup and
    the embedder's chunk-hash cache - a re-run is cheap all the way down.
    """
    try:
        rows = db.get_unchunked_documents(conn)
    except sqlite3.Error:
        logger.exception("pipeline: failed to query un-chunked documents")
        raise

    total_chunks = 0
    for processed, row in enumerate(rows, start=1):
        content_hash = row["content_hash"]
        try:
            records = chunk_document(content_hash, row["normalized_markdown"])
        except Exception:
            logger.exception("pipeline: chunking failed for document %s", content_hash)
            raise

        for rec in records:
            try:
                db.insert_chunk(
                    conn,
                    chunk_id=rec.chunk_id,
                    content_hash=rec.content_hash,
                    ordinal=rec.ordinal,
                    chunk_hash=rec.chunk_hash,
                    text=rec.text,
                    heading_path=rec.heading_path,
                )
            except sqlite3.Error:
                logger.exception("pipeline: failed to insert chunk %s", rec.chunk_id)
                raise
        total_chunks += len(records)
        if on_progress is not None:
            on_progress(processed, len(rows), 0)

    logger.info(
        "pipeline: chunked %d document(s) into %d chunk(s)", len(rows), total_chunks
    )
    return total_chunks


def _live_embedded_chunk_ids(conn: sqlite3.Connection) -> set[str]:
    """Chunk ids that are both (a) currently live - reachable through a
    non-tombstoned `document_sources` row, the same definition search()
    uses - and (b) embedded. This is exactly the set the organize engine
    clusters over."""
    return db.get_live_embedded_chunk_ids(conn)


def _hierarchy_rebuild_reason(conn: sqlite3.Connection) -> Optional[str]:
    """Organize fires only if the live chunk set changed (>=1 chunk added /
    retired / content-changed). Returns a human reason string when a
    rebuild is needed, or None for a true no-op re-sync.

    The check compares the current live+embedded chunk set against the
    set already placed in the hierarchy (`chunks.topic_id`, populated by
    the last reconcile). First run - no non-reserved topics yet - always
    rebuilds if there is anything to cluster.
    """
    try:
        has_topics = db.count_live_topics(conn) > 0
        live = _live_embedded_chunk_ids(conn)
        if not has_topics:
            return f"first organize run ({len(live)} live chunk(s))" if live else None
        placed = db.get_placed_chunk_ids(conn)
        added, removed = live - placed, placed - live
        if added or removed:
            return f"live chunk set changed (+{len(added)} / -{len(removed)})"
        return None
    except sqlite3.Error:
        logger.exception("pipeline: failed to evaluate hierarchy-rebuild condition")
        raise


def _resolve_organize_run(conn: sqlite3.Connection) -> tuple[str, int, bool]:
    """Decide the run_id + config_version_id the about-to-start organize
    attempt should use, and whether that's a resume of an existing
    `in_progress` row (docs/organize-checkpointing-DESIGN.md §4).

    - No `in_progress` row, or one under a *different* config version
      (superseded - either broken mid-run or, if completed, irrelevant
      here since a completed run never reaches this function again) ->
      start fresh: a new run_id under the current version. A stale
      `in_progress` row under an old version is deliberately left
      behind, unused (design doc §5 - retention/cleanup is still open).
    - An `in_progress` row under the *same* config version -> resume: return
      its own id, so organize()'s checkpoint lookups (keyed by run_id)
      find what it already labeled.

    Returns (run_id, config_version_id, is_resume).
    """
    current_version_id = config_versions.ensure_config_version(conn)
    existing = db.get_in_progress_organize_run(conn)
    if existing is not None and existing["config_version_id"] == current_version_id:
        return existing["id"], current_version_id, True
    return str(uuid.uuid4()), current_version_id, False


def _run_organize(
    conn: sqlite3.Connection,
    embedder: Embedder,
    llm_client: Optional[LLMClient],
    reason: Optional[str],
    run_id: str,
    on_progress: Optional[ProgressCallback] = None,
):
    """Cluster the live+embedded chunk set into a topic hierarchy.

    Raises if `reason` is falsy: the caller (`run_ingest`) must have
    already determined via `_hierarchy_rebuild_reason` that a rebuild is
    warranted before calling this - clustering plus LLM-labelling isn't
    free, and running it on an unchanged chunk set wastes a real, billed
    API call for zero net change.

    `run_id` enables per-cluster label checkpointing (§2c/§4) - a resumed
    run reuses the id of the `in_progress` row it's continuing, so
    already-labeled clusters are found and skipped rather than relabeled.
    """
    assert reason, "_run_organize called without a confirmed rebuild reason"
    logger.info("pipeline: organize/reconcile - %s (run_id=%s)", reason, run_id)

    llm_client = llm_client or OpenRouterLLM()
    # Only this construction site wraps for tool-call logging - a caller
    # that builds clustering.organize() directly with its own client
    # (tests, the debug playground) never goes through _run_organize.
    llm_client = LoggingLLMClient(llm_client, conn, run_id=run_id)
    live_chunk_ids = _live_embedded_chunk_ids(conn)

    try:
        return clustering.organize(
            conn, embedder, llm_client, restrict_to_chunk_ids=live_chunk_ids, run_id=run_id,
            on_progress=on_progress,
        )
    except IngestCancelled:
        raise
    except Exception:
        logger.exception("pipeline: organize() failed - hierarchy left untouched")
        raise


def _run_reconcile(
    conn: sqlite3.Connection,
    organize_result,
    sync_id: str,
    live_graph_path: Optional[Path],
) -> dict:
    """Reconcile a fresh clustering against the live hierarchy and persist
    the result, returning organize+reconcile stats for run_ingest's
    result dict."""
    try:
        reconcile_result = reconcile.reconcile(
            conn, organize_result, sync_id=sync_id, live_graph_path=live_graph_path
        )
    except Exception:
        logger.exception("pipeline: reconcile() failed - hierarchy left untouched")
        raise

    # Persisted (OrganizeResult is otherwise ephemeral) so get_hierarchy(),
    # possibly a separate process, can surface it. Written only now that
    # reconcile has fully committed, so the signal never gets ahead of the
    # hierarchy it describes. See docs/LOW_CONFIDENCE_DESIGN.md.
    db.set_organize_state(
        conn,
        organize_result.low_confidence,
        organize_result.low_confidence_reason,
        datetime.now(timezone.utc).isoformat(),
    )

    # Same discipline, same reason, for the run's own completion marker
    # (docs/organize-checkpointing-DESIGN.md §4): only 'completed' once
    # reconcile's persistence - including the atomic graph swap above -
    # has fully succeeded, never earlier. sync_id IS the run's id (see
    # _resolve_organize_run - reused deliberately, not a second identifier).
    db.mark_organize_run_completed(conn, sync_id, datetime.now(timezone.utc).isoformat())

    return {
        "sync_id": sync_id,
        "organize": {
            "topics": len(organize_result.nodes),
            "roots": len(organize_result.root_keys),
            "unfiled": len(organize_result.unfiled.member_chunk_ids),
            "unfiled_pct": round(organize_result.unfiled_pct, 1),
            "low_confidence": organize_result.low_confidence,
            "used_kmeans_fallback": organize_result.used_kmeans_fallback,
        },
        "reconcile": {
            "events": reconcile_result.event_counts,
            "born": len(reconcile_result.born_topic_ids),
            "tombstoned": len(reconcile_result.tombstoned_topic_ids),
            "renamed": len(reconcile_result.renamed_topic_ids),
            "reparented": len(reconcile_result.reparented_topic_ids),
            "live_topics": reconcile_result.live_topic_count,
        },
    }


def run_ingest(
    conn: sqlite3.Connection,
    connector: Connector,
    embedder: Optional[Embedder] = None,
    *,
    llm_client: Optional[LLMClient] = None,
    organize_hierarchy: bool = True,
    live_graph_path: Optional[Path] = None,
    on_phase: Optional[Callable[[str], None]] = None,
    on_batch_progress: Optional[Callable[[int, int, int, int], None]] = None,
    on_progress: Optional[ProgressCallback] = None,
    on_cancel_requested: Optional[Callable[[], bool]] = None,
    trigger: str = "cli",
) -> dict:
    """Take a connector all the way to a searchable, browsable store in one
    call: `connector.sync(conn)` (pull + normalize + dedup) -> chunk every
    not-yet-chunked document -> `embed_and_index_chunks` (embed + BM25/vector
    index) -> `_run_organize` + `_run_reconcile` (rebuild the topic hierarchy).

    Connector-agnostic: this function never imports or knows about
    `FilesystemConnector` / `DriveConnector` - a caller constructs one
    (`FilesystemConnector(root_path)`, `DriveConnector(folder_id, service=...)`)
    and hands it here.

    `embedder` defaults to `OpenRouterVoyageEmbedder()`, `llm_client` to
    `OpenRouterLLM()` - both real, billed APIs; pass stubs in tests.

    `organize_hierarchy=False` stops after the index stage - useful when a
    caller only wants to refresh the search index. When True (default), the
    organize+reconcile step still only runs if the live chunk set actually
    changed since the last one; a true no-op re-sync skips it.

    `on_phase`, if given, is called with one of "sync"/"chunk"/"embed"/
    "organize"/"reconcile" as each stage starts - coarse, phase-level
    progress. Not called for a stage this call skips (organize/
    reconcile on a no-op re-sync, or when `organize_hierarchy=False`). The
    daemon is the only current caller that passes one, to answer "what is
    the in-flight ingest doing right now" for `GET /ingest/status` - this
    function itself has and needs no opinion on where that status is
    reported. `on_batch_progress`, if given, adds batch/item-level counts
    within the embed phase specifically, and `on_progress` item counts for
    every phase but reconcile - see the module docstring above.

    Returns:
        {
            "run_id":         str,
            "sync":           <connector.sync stats>,
            "chunks_created": int,
            "embed":          <embed_and_index_chunks stats>,
            "hierarchy":      <organize+reconcile stats>  |  {"skipped": <reason>},
        }

    Errors from any stage are logged with a traceback and re-raised - no
    stage failure is swallowed. Reconcile is atomic (persists via a
    validated staged swap), so a failure there leaves the prior hierarchy
    fully intact. Whichever way this call ends - success, a raised
    exception, or `on_cancel_requested` returning True at a checkpoint -
    the `ingest_runs` row for this call's `run_id` gets a matching
    terminal status (done/failed/cancelled) before the exception (if
    any) propagates or the result is returned.

    `on_phase`, if given, is called with a human-readable phase string
    (the `PHASE_*` constants above) as each stage starts - see the
    module docstring for who this is for. `trigger` is recorded on the
    `ingest_runs` row as-is (e.g. "cli", "gui") - this function has no
    opinion on what values are meaningful, same as `connector.name`.
    """
    if embedder is None:
        embedder = OpenRouterVoyageEmbedder()

    logger.info(
        "pipeline: starting ingest connector=%s organize=%s",
        connector.name, organize_hierarchy,
    )

    run_id = str(uuid.uuid4())
    db.insert_ingest_run(
        conn, run_id, connector.name, connector.root_id, trigger,
        datetime.now(timezone.utc).isoformat(),
    )

    with _tracer.start_as_current_span(
        "ingest",
        attributes={"jarvis.connector": connector.name, "jarvis.trigger": trigger},
        record_exception=False, set_status_on_exception=False,
    ) as root_span:
        try:
            result = _run_ingest_phases(
                conn, connector, embedder, llm_client, organize_hierarchy, live_graph_path,
                on_phase, on_batch_progress, on_progress, on_cancel_requested, run_id,
            )
        except IngestCancelled as exc:
            db.finish_ingest_run(conn, run_id, db.RUN_STATUS_CANCELLED, datetime.now(timezone.utc).isoformat())
            logger.info("pipeline: ingest cancelled connector=%s run_id=%s", connector.name, run_id)
            _end_trace_on_failure(root_span, run_id, "cancelled", exc)
            raise
        except Exception as exc:
            phase_id, _ = _open_phase_spans.get(run_id, (None, None))
            error_code = classify(exc, phase_id).value
            db.finish_ingest_run(
                conn, run_id, db.RUN_STATUS_FAILED, datetime.now(timezone.utc).isoformat(),
                error_code=error_code,
            )
            _end_trace_on_failure(root_span, run_id, "failed", exc, error_code)
            raise

        root_span.set_attributes({
            "jarvis.outcome": "done",
            "jarvis.files.pulled": result["sync"].get("pulled", 0),
            "jarvis.files.failed": result["sync"].get("errors", 0),
            "jarvis.chunks.created": result["chunks_created"],
        })

    db.finish_ingest_run(conn, run_id, db.RUN_STATUS_DONE, datetime.now(timezone.utc).isoformat())
    _set_phase(on_phase, PHASE_DONE)
    return result


def _run_ingest_phases(
    conn: sqlite3.Connection,
    connector: Connector,
    embedder: Embedder,
    llm_client: Optional[LLMClient],
    organize_hierarchy: bool,
    live_graph_path: Optional[Path],
    on_phase: Optional[Callable[[str], None]],
    on_batch_progress: Optional[Callable[[int, int, int, int], None]],
    on_progress: Optional[ProgressCallback],
    on_cancel_requested: Optional[Callable[[], bool]],
    run_id: str,
) -> dict:
    """The actual phase sequence `run_ingest` records terminal status
    around - split out so that wrapping is a single try/except there,
    not duplicated at every one of this function's several raise/return
    points."""
    _begin_phase(conn, run_id, PHASE_READING_FILES, on_phase)
    try:
        sync_stats = connector.sync(conn, on_progress=on_progress)
    except Exception:
        logger.exception("pipeline: sync failed for connector=%s", connector.name)
        raise
    _end_phase(conn, run_id, PHASE_READING_FILES)

    # Only a clean pull claims the root as freshly synced - mirrors
    # sync_filesystem's own "tombstone sweep only after a fully
    # error-free pass" rule (a partial pull shouldn't read as success).
    if sync_stats.get("errors", 0) == 0:
        db.record_sync_root(
            conn, connector.name, connector.root_id, datetime.now(timezone.utc).isoformat(),
            display_name=connector.display_name,
        )
        # A root that turns out to be an ancestor of an already-tracked one
        # supersedes it - the narrower root is now covered by this sync, so
        # its own stale sync_roots row would otherwise never get cleaned up.
        other_roots = [
            row["root_id"]
            for row in db.get_sync_roots(conn)
            if row["connector"] == connector.name
        ]
        covered = connector.covered_root_ids(other_roots)
        if covered:
            db.tombstone_sync_roots(conn, connector.name, covered)

    ext_stats = sync_stats.pop("ext_stats", {})
    if ext_stats:
        db.upsert_file_type_stats(conn, ext_stats, datetime.now(timezone.utc).isoformat())
    failed_files = sync_stats.pop("failed_files", [])
    if failed_files:
        db.insert_ingest_run_file_errors(conn, run_id, failed_files)

    _check_cancel(on_cancel_requested, {"sync": sync_stats})

    _begin_phase(conn, run_id, PHASE_PROCESSING_DOCUMENTS, on_phase)
    chunks_created = _chunk_unchunked_documents(conn, on_progress)
    _end_phase(conn, run_id, PHASE_PROCESSING_DOCUMENTS, total_items=chunks_created, processed_items=chunks_created)

    _check_cancel(on_cancel_requested, {"sync": sync_stats, "chunks_created": chunks_created})

    def _on_embed_batch(current_batch: int, total_batches: int, processed_items: int, total_items: int):
        if on_batch_progress is not None:
            on_batch_progress(current_batch, total_batches, processed_items, total_items)
        if on_progress is not None:
            on_progress(processed_items, total_items, 0)

    _begin_phase(conn, run_id, PHASE_GENERATING_EMBEDDINGS, on_phase)
    try:
        embed_stats = embed_and_index_chunks(
            conn, embedder, on_batch=_on_embed_batch,
            run_id=run_id, phase=PHASE_GENERATING_EMBEDDINGS, on_cancel_requested=on_cancel_requested,
        )
    except IngestCancelled:
        raise
    except Exception:
        logger.exception("pipeline: embed_and_index_chunks failed")
        raise
    _end_phase(conn, run_id, PHASE_GENERATING_EMBEDDINGS)

    result = {
        "run_id": run_id,
        "sync": sync_stats,
        "chunks_created": chunks_created,
        "embed": embed_stats,
    }

    if organize_hierarchy:
        reason = _hierarchy_rebuild_reason(conn)
        if reason is None:
            result["hierarchy"] = {"skipped": "live chunk set unchanged (no-op re-sync)"}
            logger.info("pipeline: live chunk set unchanged - skipping organize/reconcile")
        else:
            _check_cancel(on_cancel_requested, result)
            # sync_id doubles as the organize run's id (design doc §2b -
            # reusing an existing identifier rather than inventing a
            # second one). Resolved *before* minting/reusing it: same
            # config version as an in-progress run resumes that run's id
            # (and its checkpoints); otherwise this is a fresh run,
            # exactly like every rebuild before this feature existed.
            sync_id, config_version_id, is_resume = _resolve_organize_run(conn)
            # Stamped before organize/reconcile run (not folded into their
            # eventual commit): a hierarchy rebuild is a re-sync, so
            # outstanding search cursors should go stale even if organize
            # or reconcile fails partway through.
            db.bump_sync_state(conn, sync_id)
            if not is_resume:
                db.insert_organize_run(
                    conn, sync_id, config_version_id, datetime.now(timezone.utc).isoformat(),
                )
            else:
                logger.info(
                    "pipeline: resuming in-progress organize run sync_id=%s "
                    "under unchanged config_version_id=%d", sync_id, config_version_id,
                )
            # Organize can run for an hour on a large corpus, so a cancel has
            # to land between clusters, not only after the whole phase.
            # Safe: every label is checkpointed as it's made, and the organize
            # run stays in_progress, so the next ingest resumes rather than
            # relabelling.
            def _on_organize_progress(processed: int, total: Optional[int], errors: int) -> None:
                if on_progress is not None:
                    on_progress(processed, total, errors)
                _check_cancel(on_cancel_requested, result)

            _begin_phase(conn, run_id, PHASE_ORGANIZING, on_phase)
            organize_result = _run_organize(
                conn, embedder, llm_client, reason, sync_id, _on_organize_progress,
            )
            _end_phase(conn, run_id, PHASE_ORGANIZING)

            _check_cancel(on_cancel_requested, result)

            _begin_phase(conn, run_id, PHASE_RECONCILING, on_phase)
            result["hierarchy"] = _run_reconcile(conn, organize_result, sync_id, live_graph_path)
            _end_phase(conn, run_id, PHASE_RECONCILING)
    else:
        result["hierarchy"] = {"skipped": "organize_hierarchy=False"}

    logger.info(
        "pipeline: ingest complete connector=%s result=%s", connector.name, result
    )
    return result


def remove_source(conn: sqlite3.Connection, connector: Connector) -> int:
    """Take `connector`'s root out of the library: tombstone it in
    sync_roots and tombstone the document_sources only it covers, in one
    commit. Nothing on disk or in Drive is touched, and nothing is
    hard-deleted - syncing the same root again brings it all back.

    Search stops returning the files immediately (it already skips
    tombstoned sources). Topics catch up on the next sync of any source,
    since the live chunk set has shrunk (`_hierarchy_rebuild_reason`).
    Returns how many files were taken out."""
    other_roots = [
        row["root_id"]
        for row in db.get_sync_roots(conn)
        if row["connector"] == connector.name and row["root_id"] != connector.root_id
    ]
    source_ids = connector.owned_source_ids(conn, other_roots)
    for source_id in source_ids:
        db.tombstone_document_source(conn, connector.name, source_id, commit=False)
    db.tombstone_sync_roots(conn, connector.name, [connector.root_id], commit=False)
    conn.commit()
    logger.info(
        "pipeline: removed root for connector=%s (%d source(s) tombstoned)", connector.name, len(source_ids),
    )
    return len(source_ids)
