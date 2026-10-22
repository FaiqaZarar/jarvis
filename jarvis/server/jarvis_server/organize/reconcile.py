"""Reconciliation - carry Topic identity across a manual re-sync (J-09).

Spec §6: every re-sync does a *full* re-cluster (J-08), then matches the
new clusters to the existing Topics so matched nodes keep their UUID (and,
post-MVP, their user edits). The structural changes - born / tombstone /
rename / reparent / split / merge - are *derived* from the match
cardinality, never commanded. `matching.py` does that derivation; this
module does the I/O around it:

    load prior Topics (SQLite cache)
        -> matching.match_and_derive_events(...)
        -> re-label pick (keep prior name unless the node materially changed)
        -> write `topics` rows + `topic_events`
        -> persist the reconciled tree via staged_build.build_and_swap
           (completeness-validated, atomic swap, then cache sync)

What this module does NOT do (see docs/J09_DESIGN.md):
  - decide *whether* to run (spec §5.3: the pipeline calls this only on a
    successful pull whose live chunk set changed). Not wired into
    pipeline.py in this ticket - J-08 isn't either.
  - re-cluster (J-08) or call the LLM. The re-label step is a *pick*
    between J-08's fresh label and the prior label, not a new LLM call.
"""

import json
import struct
import uuid as uuid_module
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from jarvis_server.internal import db
from jarvis_server.internal.graph_db import graph_db, staged_build
from jarvis_server.internal.logging.logging_config import get_logger
from jarvis_server.organize import clustering, config, matching

logger = get_logger(__name__)


# ---------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _floats_to_blob(vec: list[float]) -> Optional[bytes]:
    return struct.pack(f"{len(vec)}f", *vec) if vec else None


def _blob_to_floats(blob: Optional[bytes]) -> list[float]:
    if not blob:
        return []
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


# ---------------------------------------------------------------------
# result
# ---------------------------------------------------------------------

@dataclass
class ReconcileResult:
    sync_id: str
    events: list                          # matching.MutationEvent
    event_counts: dict                    # {event_type: n}
    born_topic_ids: list[str] = field(default_factory=list)
    carried_topic_ids: list[str] = field(default_factory=list)
    tombstoned_topic_ids: list[str] = field(default_factory=list)
    renamed_topic_ids: list[str] = field(default_factory=list)
    reparented_topic_ids: list[str] = field(default_factory=list)
    live_topic_count: int = 0             # non-tombstoned topics after this sync (incl. Unfiled + root)
    low_confidence: bool = False          # carried through from OrganizeResult
    graph_swapped: bool = False
    sync_stats: dict = field(default_factory=dict)


# ---------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------

def reconcile(
    sqlite_conn,
    organize_result,
    *,
    sync_id: str,
    live_graph_path: Optional[Path] = None,
) -> ReconcileResult:
    """Reconcile `organize_result` (a J-08 `OrganizeResult`) against the
    Topics currently in `sqlite_conn`, writing the new hierarchy and its
    event log. Returns a `ReconcileResult`.

    On the first sync (no prior Topics) every node is `born` - the
    matching falls out to that with no special-casing.
    """
    live_graph_path = Path(live_graph_path) if live_graph_path else graph_db.DEFAULT_GRAPH_PATH

    try:
        clustering.get_or_create_unfiled_topic(sqlite_conn)
        _ensure_root_topic(sqlite_conn)

        prior_topics = _load_prior_topics(sqlite_conn)
        new_clusters = _adapt_new_clusters(organize_result)
        new_by_key = {n.key: n for n in organize_result.nodes}

        logger.info(
            "reconcile: sync_id=%s - %d new cluster(s), %d prior topic(s), low_confidence=%s",
            sync_id, len(new_clusters), len(prior_topics), organize_result.low_confidence,
        )

        match = matching.match_and_derive_events(
            new_clusters, prior_topics, new_uuid_fn=lambda: str(uuid_module.uuid4()),
        )

        final_name, final_desc, rename_events, renamed_ids = _apply_relabel_pick(
            organize_result, match, prior_topics,
        )
        match.events.extend(rename_events)

        topic_meta = _build_topic_meta(
            organize_result, match, final_name, final_desc, prior_topics,
        )

        chunk_vectors = _load_chunk_vectors(sqlite_conn)

        _persist_topics_sqlite(sqlite_conn, topic_meta, match, prior_topics)
        _persist_events(sqlite_conn, match.events, sync_id)

        swap_stats = _persist_graph(
            sqlite_conn, live_graph_path, topic_meta, organize_result, match,
            new_by_key, chunk_vectors,
        )

        reparented_ids = sorted({e.topic_id for e in match.events if e.event_type == "reparent"})
        live_count = sum(1 for m in topic_meta.values() if not m["tombstoned"])

        result = ReconcileResult(
            sync_id=sync_id,
            events=match.events,
            event_counts=_count_events(match.events),
            born_topic_ids=sorted(match.assignment[k] for k in match.born_keys),
            carried_topic_ids=sorted(set(match.carried_from.values())),
            tombstoned_topic_ids=match.tombstoned_topic_ids,
            renamed_topic_ids=renamed_ids,
            reparented_topic_ids=reparented_ids,
            live_topic_count=live_count,
            low_confidence=organize_result.low_confidence,
            graph_swapped=True,
            sync_stats=swap_stats,
        )
        logger.info(
            "reconcile: sync_id=%s complete - events=%s live_topics=%d",
            sync_id, result.event_counts, live_count,
        )
        return result

    except Exception:
        logger.exception("reconcile: failed for sync_id=%s - hierarchy left untouched", sync_id)
        raise


# ---------------------------------------------------------------------
# reserved nodes
# ---------------------------------------------------------------------

def _ensure_root_topic(sqlite_conn) -> None:
    """Idempotently create the synthetic single root (see config)."""
    try:
        if db.get_topic(sqlite_conn, config.ROOT_TOPIC_ID) is None:
            db.insert_topic(
                sqlite_conn,
                topic_id=config.ROOT_TOPIC_ID,
                name=config.ROOT_TOPIC_NAME,
                description=config.ROOT_TOPIC_DESCRIPTION,
                level=0,
                reserved=True,
            )
            logger.info("reconcile: created synthetic root topic %s", config.ROOT_TOPIC_ID)
    except Exception:
        logger.exception("reconcile: failed to ensure the synthetic root topic")
        raise


# ---------------------------------------------------------------------
# load prior state
# ---------------------------------------------------------------------

def _load_prior_topics(sqlite_conn) -> list[matching.PriorTopic]:
    """Prior Topics as of the last successful sync, from the SQLite cache.
    `get_live_topics` already excludes the reserved nodes (Unfiled, root)
    and tombstoned nodes - none of which are ever match candidates."""
    out: list[matching.PriorTopic] = []
    for row in db.get_live_topics(sqlite_conn):
        members = db.get_chunk_ids_for_topic(sqlite_conn, row["id"])
        ancestor_path = json.loads(row["ancestor_path"] or "[]")
        parent_id = ancestor_path[-2] if len(ancestor_path) >= 2 else None
        # a prior parent that is the synthetic root reads as "no parent"
        # for matching purposes - reparent detection compares real topics.
        if parent_id == config.ROOT_TOPIC_ID:
            parent_id = None
        out.append(matching.PriorTopic(
            topic_id=row["id"],
            centroid=_blob_to_floats(row["centroid"]),
            member_chunk_ids=members,
            name=row["name"],
            description=row["description"] or "",
            parent_topic_id=parent_id,
            version=row["version"] if "version" in row.keys() else 1,
        ))
    return out


def _adapt_new_clusters(organize_result) -> list[matching.NewCluster]:
    return [
        matching.NewCluster(
            key=n.key,
            centroid=list(n.centroid),
            member_chunk_ids=set(n.member_chunk_ids),
            label=n.label,
            description=n.description,
            parent_key=n.parent_key,
        )
        for n in organize_result.nodes
    ]


def _load_chunk_vectors(sqlite_conn) -> dict[str, list[float]]:
    chunk_ids, matrix = clustering._load_chunk_vectors(sqlite_conn)
    return {cid: [float(x) for x in matrix[i]] for i, cid in enumerate(chunk_ids)}


# ---------------------------------------------------------------------
# re-label discipline (spec §6) - a pick, not an LLM call
# ---------------------------------------------------------------------

def _apply_relabel_pick(organize_result, match, prior_topics):
    """For each surviving new node decide its final name/description:
    a carried-forward node that did NOT materially change keeps its prior
    label (fights name churn); everything else takes J-08's fresh label.
    Emits a `rename` event whenever the final name differs from the prior
    name of a carried node."""
    prior_by_id = {p.topic_id: p for p in prior_topics}
    final_name: dict[str, str] = {}
    final_desc: dict[str, str] = {}
    rename_events: list[matching.MutationEvent] = []
    renamed_ids: list[str] = []

    for n in organize_result.nodes:
        carried_id = match.carried_from.get(n.key)
        keep_prior = carried_id is not None and n.key not in match.materially_changed_keys
        if keep_prior:
            p = prior_by_id[carried_id]
            final_name[n.key], final_desc[n.key] = p.name, p.description
        else:
            final_name[n.key], final_desc[n.key] = n.label, n.description

        if carried_id is not None:
            p = prior_by_id[carried_id]
            if final_name[n.key] != p.name:
                rename_events.append(matching.MutationEvent(
                    "rename", match.assignment[n.key],
                    {"from": p.name, "to": final_name[n.key]},
                ))
                renamed_ids.append(match.assignment[n.key])

    return final_name, final_desc, rename_events, sorted(renamed_ids)


# ---------------------------------------------------------------------
# assemble the reconciled tree
# ---------------------------------------------------------------------

def _build_topic_meta(organize_result, match, final_name, final_desc, prior_topics):
    """uuid -> {name, description, centroid, parent_uuid, level, version,
    reserved, tombstoned}. Covers every node in the reconciled live tree
    plus the tombstoned ones (so the SQLite writer has everything)."""
    prior_by_id = {p.topic_id: p for p in prior_topics}
    meta: dict[str, dict] = {}

    # --- surviving new nodes ---
    for n in organize_result.nodes:
        tid = match.assignment[n.key]
        parent_uuid = match.assignment.get(n.parent_key) if n.parent_key else config.ROOT_TOPIC_ID
        changed = (
            n.key in match.materially_changed_keys
            or any(e.topic_id == tid for e in match.events)
        )
        version = _carried_version(tid, match, prior_by_id, changed)
        meta[tid] = {
            "name": final_name[n.key],
            "description": final_desc[n.key],
            "centroid": list(n.centroid),
            "parent_uuid": parent_uuid,
            "level": 0,  # filled by _assign_levels
            "version": version,
            "reserved": False,
            "tombstoned": False,
            "is_new": n.key in match.born_keys,
        }

    # --- Unfiled: reserved child of the root, fixed label ---
    meta[config.UNFILED_TOPIC_ID] = {
        "name": config.UNFILED_TOPIC_NAME,
        "description": config.UNFILED_TOPIC_DESCRIPTION,
        "centroid": list(organize_result.unfiled.centroid or []),
        "parent_uuid": config.ROOT_TOPIC_ID,
        "level": 1,
        "version": 1,
        "reserved": True,
        "tombstoned": False,
        "is_new": False,
    }

    # --- synthetic root: reserved, no parent ---
    meta[config.ROOT_TOPIC_ID] = {
        "name": config.ROOT_TOPIC_NAME,
        "description": config.ROOT_TOPIC_DESCRIPTION,
        "centroid": [],
        "parent_uuid": None,
        "level": 0,
        "version": 1,
        "reserved": True,
        "tombstoned": False,
        "is_new": False,
    }

    # --- tombstoned priors: kept for the SQLite writer, not in the graph ---
    for tid in match.tombstoned_topic_ids:
        p = prior_by_id.get(tid)
        superseded = _superseded_by(tid, match.events)
        meta[tid] = {
            "name": p.name if p else "(tombstoned)",
            "description": p.description if p else "",
            "centroid": p.centroid if p else [],
            "parent_uuid": None,
            "level": 0,
            "version": (_prior_version(tid, prior_by_id) + 1),
            "reserved": False,
            "tombstoned": True,
            "superseded_by": superseded,
            "is_new": False,
        }

    _assign_levels(meta)
    return meta


def _carried_version(tid, match, prior_by_id, changed) -> int:
    if tid not in match.carried_from.values():
        return 1  # born
    base = _prior_version(tid, prior_by_id)
    return base + 1 if changed else base


def _prior_version(tid, prior_by_id) -> int:
    p = prior_by_id.get(tid)
    return p.version if p else 1


def _superseded_by(tid: str, events) -> Optional[str]:
    for e in events:
        if e.event_type == "tombstone" and e.topic_id == tid and e.detail.get("merged_into"):
            return e.detail["merged_into"]
    return None


def _assign_levels(meta: dict) -> None:
    """Breadth-first level assignment from the synthetic root."""
    children: dict[str, list[str]] = {}
    for tid, m in meta.items():
        if m["tombstoned"]:
            continue
        if m["parent_uuid"]:
            children.setdefault(m["parent_uuid"], []).append(tid)
    frontier = [(config.ROOT_TOPIC_ID, 0)]
    seen = set()
    while frontier:
        tid, depth = frontier.pop(0)
        if tid in seen:
            continue
        seen.add(tid)
        if tid in meta:
            meta[tid]["level"] = depth
        for child in children.get(tid, []):
            frontier.append((child, depth + 1))


# ---------------------------------------------------------------------
# persist: SQLite topics + events
# ---------------------------------------------------------------------

def _persist_topics_sqlite(sqlite_conn, topic_meta, match, prior_topics) -> None:
    existing = db.get_all_topic_ids(sqlite_conn)
    for tid, m in topic_meta.items():
        if tid in (config.ROOT_TOPIC_ID, config.UNFILED_TOPIC_ID):
            # reserved rows already exist (ensured up front); refresh centroid/level only
            db.update_topic(
                sqlite_conn, tid,
                name=m["name"], description=m["description"],
                centroid=_floats_to_blob(m["centroid"]),
                level=m["level"], version=m["version"],
            )
            continue
        if m["tombstoned"]:
            db.mark_topic_tombstoned(sqlite_conn, tid, superseded_by=m.get("superseded_by"))
            continue
        if tid in existing:
            db.update_topic(
                sqlite_conn, tid,
                name=m["name"], description=m["description"],
                centroid=_floats_to_blob(m["centroid"]),
                level=m["level"], version=m["version"],
            )
        else:
            db.insert_topic(
                sqlite_conn, topic_id=tid, name=m["name"], description=m["description"],
                centroid=_floats_to_blob(m["centroid"]), level=m["level"], reserved=False,
            )
            if m["version"] != 1:
                db.update_topic(
                    sqlite_conn, tid,
                    name=m["name"], description=m["description"],
                    centroid=_floats_to_blob(m["centroid"]),
                    level=m["level"], version=m["version"],
                )


def _persist_events(sqlite_conn, events, sync_id: str) -> None:
    now = _now_iso()
    for e in events:
        db.insert_topic_event(
            sqlite_conn,
            topic_id=e.topic_id,
            sync_id=sync_id,
            event_type=e.event_type,
            created_at=now,
            detail_json=json.dumps(e.detail) if e.detail else None,
        )


# ---------------------------------------------------------------------
# persist: the graph (validated atomic swap + cache sync)
# ---------------------------------------------------------------------

def _persist_graph(sqlite_conn, live_graph_path, topic_meta, organize_result, match,
                   new_by_key, chunk_vectors) -> dict:
    unfiled_id = config.UNFILED_TOPIC_ID
    all_chunk_ids = db.get_all_chunk_ids(sqlite_conn)

    def populate(gconn) -> None:
        # --- topic nodes (live only; tombstoned leave the tree) ---
        for tid, m in topic_meta.items():
            if m["tombstoned"]:
                continue
            graph_db.insert_topic(
                gconn, tid, m["name"], m["description"],
                level=m["level"], version=m["version"], reserved=m["reserved"],
            )
        # --- chunk anchors for every chunk SQLite knows about ---
        for cid in all_chunk_ids:
            graph_db.insert_chunk_anchor(gconn, cid)
        # --- PARENT_OF ---
        for tid, m in topic_meta.items():
            if m["tombstoned"] or not m["parent_uuid"]:
                continue
            graph_db.set_parent(gconn, tid, m["parent_uuid"])
        # --- CONTAINS: direct placements from J-08, then Unfiled, then a safety net ---
        placed: set[str] = set()
        for key, node in new_by_key.items():
            tid = match.assignment[key]
            centroid = topic_meta[tid]["centroid"]
            for cid in node.direct_chunk_ids:
                conf = max(0.0, _cosine(chunk_vectors.get(cid, []), centroid))
                graph_db.place_chunk(gconn, cid, tid, conf)
                placed.add(cid)
        for cid in organize_result.unfiled.member_chunk_ids:
            graph_db.place_chunk(gconn, cid, unfiled_id, 0.0)
            placed.add(cid)
        for cid in all_chunk_ids - placed:
            logger.warning(
                "reconcile: chunk %s was not placed by organize - routing to Unfiled", cid,
            )
            graph_db.place_chunk(gconn, cid, unfiled_id, 0.0)

    return staged_build.build_and_swap(sqlite_conn, live_graph_path, populate)


# ---------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------

def _count_events(events) -> dict:
    out: dict[str, int] = {}
    for e in events:
        out[e.event_type] = out.get(e.event_type, 0) + 1
    return out
