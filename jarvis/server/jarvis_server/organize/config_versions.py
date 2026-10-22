"""Config versioning for the organize engine (docs/organize-checkpointing-DESIGN.md §2a/§6).

A version is a snapshot of `config.snapshot()` - the UMAP/HDBSCAN/
placement/labeling tunables - recorded in `organize_config_versions`.
"The current version" is whichever row was inserted most recently
(`db.get_current_config_version`); resume logic (pipeline.py) compares
an in-progress run's recorded version against this to decide whether to
continue from checkpoints or start fresh (design doc §4).

Two ways a row gets inserted, and only two - never a third, automatic
one:

  1. `record_new_config_version()` - the deliberate action, called when
     someone has actually changed a tunable in config.py and wants that
     recorded as a new version. Not wired into any pipeline path -
     nothing calls this except a person or a script doing it on purpose.
  2. `ensure_config_version()`'s one-time bootstrap - if the table has
     never had a row at all, it inserts one from whatever config.py
     currently holds, so the very first organize run in this database's
     history has *some* version to record itself under. This is seeding
     an undefined value, not re-versioning an existing one - it never
     fires again once a single row exists, regardless of whether
     config.py's live values later drift from that first row (design
     doc §5, open question #1 - drift detection is a separate, still-
     open decision, not addressed by this bootstrap).
"""

from datetime import datetime, timezone
import json

from jarvis_server.internal import db
from jarvis_server.internal.logging.logging_config import get_logger
from jarvis_server.organize import config

logger = get_logger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_new_config_version(conn, note: str = None) -> int:
    """Deliberately record config.py's current tunables as a new version.
    Always inserts, regardless of whether it matches the prior row -
    call this only when a tunable was actually, intentionally changed."""
    params_json = json.dumps(config.snapshot(), sort_keys=True)
    version_id = db.insert_config_version(conn, params_json, _now_iso(), note=note)
    logger.info("organize: recorded new config version id=%d note=%r", version_id, note)
    return version_id


def ensure_config_version(conn) -> int:
    """Return the current config version id, bootstrapping version 1
    from config.py's live values if the table has never had a row."""
    row = db.get_current_config_version(conn)
    if row is not None:
        return row["id"]
    return record_new_config_version(conn, note="bootstrap: first organize run in this database")
