"""Shared fixtures.

Database-backed tests run against a real Postgres (the one from `make run`),
in a dedicated `gateway_test` database created on first use and migrated
with Alembic. This is deliberate: the idempotency guarantee rests on
Postgres' ON CONFLICT behaviour and the migration is code worth testing.
SQLite would test a different database.

If Postgres is not reachable the session fails immediately with a pointer
to `make run`, rather than skipping and reporting green.
"""

import os
import time

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from gateway.api.app import create_app
from gateway.api.signature import compute_signature
from gateway.config import Settings
from gateway.db import make_session_factory
from gateway.queue import MemoryQueue

ADMIN_URL = os.environ.get(
    "TEST_ADMIN_DATABASE_URL", "postgresql+psycopg://gateway:gateway@localhost:5432/gateway"
)
TEST_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://gateway:gateway@localhost:5432/gateway_test"
)

SALESFORCE_SECRET = "sf-test-secret"
HUBSPOT_SECRET = "hs-test-secret"


def _ensure_test_database() -> None:
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = 'gateway_test'")
            ).scalar()
            if not exists:
                conn.execute(text("CREATE DATABASE gateway_test"))
    except Exception as exc:  # noqa: BLE001
        pytest.fail(
            f"Postgres not reachable at {ADMIN_URL}. Run `make run` first. "
            f"({exc.__class__.__name__})",
            pytrace=False,
        )
    finally:
        admin.dispose()


@pytest.fixture(scope="session")
def engine() -> Engine:
    _ensure_test_database()
    eng = create_engine(TEST_URL)
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_URL)
    # Down then up: proves the migration is reversible and starts every
    # session from a known schema, even after a migration file changed.
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    # Per-test isolation by truncation rather than per-test transactions:
    # the code under test opens its own transactions (session_factory.begin())
    # and we want those to really commit, exactly as in production.
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE event_transitions, events"))
    return make_session_factory(engine)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url=TEST_URL,
        webhook_secret_salesforce=SALESFORCE_SECRET,
        webhook_secret_hubspot=HUBSPOT_SECRET,
        webhook_timestamp_tolerance_seconds=300,
        queue_backend="memory",
    )


@pytest.fixture
def queue() -> MemoryQueue:
    return MemoryQueue()


@pytest.fixture
def client(
    settings: Settings, session_factory: sessionmaker[Session], queue: MemoryQueue
) -> TestClient:
    return TestClient(create_app(settings, session_factory, queue))


def signed_headers(secret: str, body: bytes, timestamp: int | None = None) -> dict[str, str]:
    """Headers a correctly-behaving CRM would send."""
    ts = str(int(time.time()) if timestamp is None else timestamp)
    return {
        "X-Webhook-Timestamp": ts,
        "X-Webhook-Signature": compute_signature(secret, ts, body),
        "Content-Type": "application/json",
    }
