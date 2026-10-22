"""Migration runner (library) - applies numbered .sql files from this
package's own `migrations/` in order, tracking which have already run in
a `schema_migrations` table so re-running is idempotent. Same shape as
jarvis_server's own `internal/migrate.py`, translated to psycopg.
"""

import os
from pathlib import Path

import psycopg

from jarvis_cloud_server.internal.logging.logging_config import get_logger

logger = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


def _ensure_migrations_table(conn: psycopg.Connection) -> None:
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename   TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        conn.commit()
    except psycopg.Error:
        conn.rollback()
        logger.exception("Failed to create schema_migrations table")
        raise


def _already_applied(conn: psycopg.Connection) -> set[str]:
    try:
        rows = conn.execute("SELECT filename FROM schema_migrations").fetchall()
        return {row[0] for row in rows}
    except psycopg.Error:
        logger.exception("Failed to query schema_migrations")
        raise


def run_migrations(dsn: str) -> None:
    """Apply every pending .sql migration in MIGRATIONS_DIR, in filename order."""
    try:
        conn = psycopg.connect(dsn)
    except psycopg.Error:
        logger.exception("Failed to open database connection")
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
                conn.execute(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s)",
                    (migration_file.name,),
                )
                conn.commit()
                logger.info("Applied migration: %s", migration_file.name)
            except psycopg.Error:
                conn.rollback()
                logger.exception("Failed to apply migration: %s", migration_file.name)
                raise
    finally:
        conn.close()


if __name__ == "__main__":
    # Run as `python -m jarvis_cloud_server.internal.migrate` before the
    # server starts (see Dockerfile) - reads the same env var the server
    # itself connects with, so there's one source of truth for which
    # database gets migrated. load_dotenv() mirrors server.py's own call:
    # a no-op in Docker/Railway (vars are already in the environment,
    # there's no .env file in the image), but required for `make
    # cloud-migrate` to pick up jarvis_cloud/.env locally.
    from dotenv import load_dotenv

    load_dotenv()
    _dsn = os.environ["JARVIS_CLOUD_DATABASE_URL"]
    run_migrations(_dsn)
