"""Database access layer.

Thin wrappers around sqlite3, one module per table (see database-access.md
for the package layout and the read_query/write_query middleware every
table module uses). This __init__ re-exports every name so existing call
sites - `from jarvis_server.internal import db` then `db.get_connection(...)`,
`db.insert_document(...)`, etc. - are unaffected by which file a function
actually lives in.
"""

from jarvis_server.internal.db.connection import DEFAULT_DB_PATH, get_connection
from jarvis_server.internal.db.documents import get_document, get_unchunked_documents, insert_document
from jarvis_server.internal.db.document_sources import (
    get_active_document_source_modified_ats,
    get_active_document_sources,
    get_active_source_ids_for_connector,
    get_document_sources_for_document,
    get_live_sources_for_document,
    insert_document_source,
    rename_document_source,
    tombstone_document_source,
    touch_document_source_seen,
)
from jarvis_server.internal.db.connector_state import get_connector_state, set_connector_state
from jarvis_server.internal.db.sync_roots import get_sync_roots, record_sync_root, tombstone_sync_roots
from jarvis_server.internal.db.ingest_runs import (
    STATUS_CANCELLED as RUN_STATUS_CANCELLED,
    STATUS_DONE as RUN_STATUS_DONE,
    STATUS_FAILED as RUN_STATUS_FAILED,
    STATUS_IN_PROGRESS as RUN_STATUS_IN_PROGRESS,
    STATUS_INTERRUPTED as RUN_STATUS_INTERRUPTED,
    finish_ingest_run,
    get_ingest_run,
    get_recent_ingest_runs,
    insert_ingest_run,
    mark_orphaned_ingest_runs_interrupted,
)
from jarvis_server.internal.db.ingest_run_file_errors import (
    get_ingest_run_file_errors,
    insert_ingest_run_file_errors,
)
from jarvis_server.internal.db.ingest_run_phases import (
    STATUS_DONE as PHASE_STATUS_DONE,
    STATUS_FAILED as PHASE_STATUS_FAILED,
    STATUS_IN_PROGRESS as PHASE_STATUS_IN_PROGRESS,
    finish_ingest_run_phase,
    get_ingest_run_phases,
    insert_ingest_run_phase,
    set_ingest_run_phase_progress,
)
from jarvis_server.internal.db.chunks import (
    chunk_exists,
    chunk_has_live_source,
    get_all_chunk_ids,
    get_candidate_chunk_ids,
    get_chunk_by_id,
    get_chunk_ids_for_topic,
    get_chunk_ids_for_topic_ordered,
    get_chunks_for_document,
    get_live_embedded_chunk_ids,
    get_placed_chunk_ids,
    insert_chunk,
    update_chunk_placement,
)
from jarvis_server.internal.db.embedding import (
    bump_sync_state,
    count_chunk_vectors,
    get_all_chunk_texts,
    get_all_chunk_vectors,
    get_chunk_ids_sample,
    get_chunk_vector,
    get_current_sync_id,
    get_embedded_chunk_ids,
    get_fts5_ranked_chunk_ids,
    get_vector_ranked_chunk_ids,
    insert_chunk_fts,
    insert_chunk_vector,
)
from jarvis_server.internal.db.topics import (
    count_live_topics,
    get_all_topic_ids,
    get_live_topics,
    get_ordered_topics_by_id,
    get_related_topic_candidates,
    get_topic,
    get_topic_ids_by_name,
    insert_topic,
    mark_topic_tombstoned,
    update_topic,
    update_topic_cache,
)
from jarvis_server.internal.db.topic_events import get_events_for_topic, insert_topic_event
from jarvis_server.internal.db.organize_state import get_organize_state, set_organize_state
from jarvis_server.internal.db.organize_config_versions import (
    get_current_config_version,
    insert_config_version,
)
from jarvis_server.internal.db.organize_runs import (
    get_in_progress_organize_run,
    insert_organize_run,
    mark_organize_run_completed,
)
from jarvis_server.internal.db.organize_cluster_checkpoints import (
    get_cluster_checkpoint,
    insert_cluster_checkpoint,
)
from jarvis_server.internal.db.tool_calls import get_recent_tool_calls, insert_tool_call
from jarvis_server.internal.db.file_type_stats import (
    get_file_type_stats,
    get_processing_totals,
    upsert_file_type_stats,
)

__all__ = [
    "DEFAULT_DB_PATH",
    "get_connection",
    "get_document",
    "get_unchunked_documents",
    "insert_document",
    "get_active_document_source_modified_ats",
    "get_active_document_sources",
    "get_active_source_ids_for_connector",
    "get_document_sources_for_document",
    "get_live_sources_for_document",
    "insert_document_source",
    "rename_document_source",
    "tombstone_document_source",
    "touch_document_source_seen",
    "get_connector_state",
    "set_connector_state",
    "get_sync_roots",
    "record_sync_root",
    "tombstone_sync_roots",
    "RUN_STATUS_IN_PROGRESS",
    "RUN_STATUS_DONE",
    "RUN_STATUS_FAILED",
    "RUN_STATUS_CANCELLED",
    "RUN_STATUS_INTERRUPTED",
    "insert_ingest_run",
    "finish_ingest_run",
    "get_ingest_run",
    "get_recent_ingest_runs",
    "mark_orphaned_ingest_runs_interrupted",
    "insert_ingest_run_file_errors",
    "get_ingest_run_file_errors",
    "PHASE_STATUS_IN_PROGRESS",
    "PHASE_STATUS_DONE",
    "PHASE_STATUS_FAILED",
    "insert_ingest_run_phase",
    "set_ingest_run_phase_progress",
    "finish_ingest_run_phase",
    "get_ingest_run_phases",
    "chunk_exists",
    "chunk_has_live_source",
    "get_all_chunk_ids",
    "get_candidate_chunk_ids",
    "get_chunk_by_id",
    "get_chunk_ids_for_topic",
    "get_chunk_ids_for_topic_ordered",
    "get_chunks_for_document",
    "get_live_embedded_chunk_ids",
    "get_placed_chunk_ids",
    "insert_chunk",
    "update_chunk_placement",
    "bump_sync_state",
    "count_chunk_vectors",
    "get_all_chunk_texts",
    "get_all_chunk_vectors",
    "get_chunk_ids_sample",
    "get_chunk_vector",
    "get_current_sync_id",
    "get_embedded_chunk_ids",
    "get_fts5_ranked_chunk_ids",
    "get_vector_ranked_chunk_ids",
    "insert_chunk_fts",
    "insert_chunk_vector",
    "count_live_topics",
    "get_all_topic_ids",
    "get_live_topics",
    "get_ordered_topics_by_id",
    "get_related_topic_candidates",
    "get_topic",
    "get_topic_ids_by_name",
    "insert_topic",
    "mark_topic_tombstoned",
    "update_topic",
    "update_topic_cache",
    "get_events_for_topic",
    "insert_topic_event",
    "get_organize_state",
    "set_organize_state",
    "get_current_config_version",
    "insert_config_version",
    "get_in_progress_organize_run",
    "insert_organize_run",
    "mark_organize_run_completed",
    "get_cluster_checkpoint",
    "insert_cluster_checkpoint",
    "insert_tool_call",
    "get_recent_tool_calls",
    "upsert_file_type_stats",
    "get_file_type_stats",
    "get_processing_totals",
]
