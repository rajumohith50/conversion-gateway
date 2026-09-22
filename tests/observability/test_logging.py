"""Redaction happens at the logger, so a careless call site cannot leak."""

import io
import json
import logging
from typing import Any

import pytest
import structlog

from gateway.observability.logging import REDACTED, configure_logging, get_logger, redact_pii


def test_processor_scrubs_sensitive_keys_and_patterns() -> None:
    event: dict[str, Any] = {
        "event": "ingest: raw email is mohith@gmail.com, phone +1 415 555 2671",
        "email": "leaky@example.com",
        "payload": {"Lead": {"Email": "leaky@example.com", "Id": "00Q1"}},
        "identifiers": {"email": "x@y.com"},
        "note": "call 4155552671 later",
        "hashed_email": "a" * 64,  # a digest is fine
        "event_id": "salesforce:e-1",
        "count": 3,
    }
    out = redact_pii(None, "info", event)

    assert "mohith@gmail.com" not in out["event"]
    assert "415 555 2671" not in out["event"]
    assert out["email"] == REDACTED
    assert out["payload"]["Lead"] == {"Email": REDACTED, "Id": "00Q1"}
    assert out["identifiers"] == REDACTED
    assert out["note"] == f"call {REDACTED} later"
    assert out["hashed_email"] == "a" * 64
    assert out["event_id"] == "salesforce:e-1"
    assert out["count"] == 3


def test_digest_strings_are_not_mistaken_for_phone_numbers() -> None:
    digest = "0123456789abcdef" * 4
    out = redact_pii(None, "info", {"event": "x", "hash": digest})
    assert out["hash"] == digest


@pytest.fixture
def json_log_lines() -> Any:
    """Configure logging for real, capture stdout as JSON lines."""
    buf = io.StringIO()
    configure_logging("INFO", json_output=True)
    logging.getLogger().handlers[0].stream = buf  # type: ignore[attr-defined]

    def read() -> list[dict[str, Any]]:
        return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]

    yield read
    structlog.reset_defaults()
    logging.getLogger().handlers[:] = []


def test_logging_a_dict_with_a_raw_email_emits_a_redacted_value(json_log_lines: Any) -> None:
    log = get_logger("test")
    log.info("webhook received", payload={"email": "person@example.com", "phone": "(415) 555-2671"})

    [line] = json_log_lines()
    assert line["event"] == "webhook received"
    assert line["payload"] == {"email": REDACTED, "phone": REDACTED}
    assert "person@example.com" not in json.dumps(line)
    assert "555" not in json.dumps(line)


def test_stdlib_loggers_are_redacted_too(json_log_lines: Any) -> None:
    logging.getLogger("third.party").warning("connecting as someone@example.com")
    [line] = json_log_lines()
    assert line["level"] == "warning"
    assert line["logger"] == "third.party"
    assert "someone@example.com" not in line["event"]


def test_correlation_id_from_contextvars_is_on_every_line(json_log_lines: Any) -> None:
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(correlation_id="abc123")
    get_logger("a").info("first")
    get_logger("b").info("second", extra_field=1)
    structlog.contextvars.clear_contextvars()
    get_logger("c").info("third")

    lines = json_log_lines()
    assert [line.get("correlation_id") for line in lines] == ["abc123", "abc123", None]
    assert lines[1]["extra_field"] == 1


def test_pydantic_models_are_scrubbed_when_logged(json_log_lines: Any) -> None:
    from gateway.normalise import RawIdentifiers

    get_logger("t").info("oops", raw=RawIdentifiers(email="a@b.com", country="US"))
    [line] = json_log_lines()
    assert line["raw"] == REDACTED  # key "raw" is sensitive outright
    get_logger("t").info("oops2", record=RawIdentifiers(email="a@b.com", country="US"))
    line = json_log_lines()[-1]
    assert line["record"]["email"] == REDACTED
    assert line["record"]["country"] == "US"
