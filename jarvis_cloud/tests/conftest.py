"""Shared fixtures for jarvis_cloud_server's tests - a real local
Postgres test database (JARVIS_CLOUD_TEST_DATABASE_URL), migrated once
per session and truncated between tests, plus a TestClient wired to it.
Same "real thing, not a mock" philosophy as tests/test_daemon.py, applied
to Postgres instead of a tmp-file SQLite db.
"""

import os

import pytest
from dotenv import load_dotenv
from starlette.testclient import TestClient

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

TEST_DSN = os.environ.get("JARVIS_CLOUD_TEST_DATABASE_URL")
if not TEST_DSN:
    pytest.skip(
        "JARVIS_CLOUD_TEST_DATABASE_URL is not set - see jarvis_cloud/.env.example",
        allow_module_level=True,
    )

# Must happen before anything imports jarvis_cloud_server.internal.db.connection,
# since get_pool() reads this env var (lazily, on first checkout - not at
# import time), so setting it here is enough to redirect the whole
# service at the test database, no monkeypatching required.
os.environ["JARVIS_CLOUD_DATABASE_URL"] = TEST_DSN

from jarvis_cloud_server import server as server_mod  # noqa: E402
from jarvis_cloud_server.auth.email import InMemoryEmailSender  # noqa: E402
from jarvis_cloud_server.internal.db import connection as db_connection  # noqa: E402
from jarvis_cloud_server.internal.migrate import run_migrations  # noqa: E402

run_migrations(TEST_DSN)

_TABLES = ["feedback", "auto_error_reports", "sessions", "login_requests", "users"]


@pytest.fixture(autouse=True)
def _clean_db():
    """Truncate every table before each test - cheap and deterministic,
    the Postgres analog of SQLite's tmp-file-per-test isolation."""
    with db_connection.get_connection() as conn:
        conn.execute(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE")
        conn.commit()
    yield


@pytest.fixture
def email_sender(monkeypatch):
    sender = InMemoryEmailSender()
    monkeypatch.setattr(server_mod, "_email_sender", sender)
    return sender


@pytest.fixture
def client(email_sender):
    return TestClient(server_mod.app)
