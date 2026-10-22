"""Migration runner (library) - the CLI entrypoint is `jarvis.cmd.migrate`
(`python -m jarvis.cmd.migrate`), a thin wrapper around `run_migrations`
below. Applies numbered .sql files from this package's own
`migrations/` in order, tracking which have already run in a
`schema_migrations` table so re-running is idempotent.
"""

import sqlite3
from pathlib import Path

import sqlite_vec

from jarvis_server.internal.logging.logging_config import get_logger

logger = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"
# Reaches the repo root (5 levels up from
# jarvis/server/jarvis_server/internal/migrate.py) - the same "default
# unless overridden" convenience jarvis_server.internal.db.connection's
# own DEFAULT_DB_PATH and jarvis_server.internal.graph_db.graph_db's
# DEFAULT_GRAPH_PATH already use, kept in sync with those two after the
# server/client isolation move (was parents[2] before this package
# moved from jarvis/cmd/migrate.py - fixed here, not left silently
# broken by the relocation).
DEFAULT_DB_PATH = Path(__file__).resolve().parents[4] / "jarvis.db"


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename    TEXT PRIMARY KEY,
                applied_at   TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        logger.exception("Failed to create schema_migrations table")
        raise


def _already_applied(conn: sqlite3.Connection) -> set[str]:
    try:
        rows = conn.execute("SELECT filename FROM schema_migrations").fetchall()
        return {row[0] for row in rows}
    except sqlite3.Error:
        logger.exception("Failed to query schema_migrations")
        raise


def run_migrations(db_path: Path = DEFAULT_DB_PATH) -> None:
    """Apply every pending .sql migration in MIGRATIONS_DIR, in filename order."""
    try:
        conn = sqlite3.connect(db_path)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except sqlite3.Error:
        logger.exception("Failed to open database connection db_path=%s", db_path)
        raise
    try:
        _ensure_migrations_table(conn)
        applied = _already_applied(conn)

        migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        if not migration_files:
            logger.warning("No migration files found in %s", MIGRATIONS_DIR)
            return

        for migration_file in migration_files:
            if migration_file.name in applied:
                logger.info("Skipping already-applied migration: %s", migration_file.name)
                continue
            try:
                sql = migration_file.read_text()
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (?)",
                    (migration_file.name,),
                )
                conn.commit()
                logger.info("Applied migration: %s", migration_file.name)
            except sqlite3.Error:
                conn.rollback()
                logger.exception("Failed to apply migration: %s", migration_file.name)
                raise
    finally:
        conn.close()
