"""create_app_from_env builds a working app from Settings alone."""

from fastapi.testclient import TestClient

from gateway.api.app import create_app_from_env
from tests.conftest import TEST_URL


def test_create_app_from_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    monkeypatch.setenv("WEBHOOK_SECRET_SALESFORCE", "x")
    app = create_app_from_env()
    assert app.state.settings.webhook_secret_salesforce == "x"
    assert TestClient(app).get("/readyz").status_code == 200
