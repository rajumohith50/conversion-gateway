"""create_app_from_env builds a working app from Settings alone."""

from fastapi.testclient import TestClient
from sqlalchemy import Engine

from gateway.api.app import create_app_from_env
from tests.conftest import TEST_URL


def test_create_app_from_env(monkeypatch, engine: Engine) -> None:  # type: ignore[no-untyped-def]
    # `engine` is requested only for its side effect: it creates and
    # migrates the test database. This file sorts first in the suite, and
    # without that dependency the app's own engine points at a database
    # that does not exist yet on a fresh Postgres, so /readyz is 503.
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    monkeypatch.setenv("WEBHOOK_SECRET_SALESFORCE", "x")
    monkeypatch.setenv("QUEUE_BACKEND", "memory")
    app = create_app_from_env()
    assert app.state.settings.webhook_secret_salesforce == "x"
    assert TestClient(app).get("/readyz").status_code == 200
