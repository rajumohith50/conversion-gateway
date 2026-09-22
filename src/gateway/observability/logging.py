"""structlog configuration: JSON lines, correlation id, PII redaction.

Redaction is a processor on the logger, not a helper at call sites. A call
site that logs a whole payload dict "for debugging" still cannot leak an
email, because the value is scrubbed before it is rendered. The same
processor chain is installed on the stdlib root logger, so lines from
httpx, uvicorn and SQLAlchemy get the same treatment.

The correlation id lives in structlog's contextvars. Whoever starts a unit
of work (the ingest route, the worker per message, the uploader per batch)
binds it once; every log line inside that context carries it without
being passed around.
"""

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

# Field names whose values are PII by definition. Matched case-insensitively
# as substrings so "Lead.Email", "hashed_email"-adjacent raw fields, and
# "identifiers" blocks are all caught.
_SENSITIVE_KEYS = (
    "email",
    "phone",
    "given_name",
    "family_name",
    "first_name",
    "last_name",
    "firstname",
    "lastname",
    "street",
    "address",
    "identifiers",
    "raw",
    "body",
)
_SAFE_KEY_PREFIXES = ("hashed_",)

# Values that look like PII regardless of the key they are under. The phone
# pattern is deliberately loose: 7+ digits with optional separators. Better
# to redact a harmless number than to leak a real one.
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<![0-9a-f])\+?\d[\d\s().\-]{6,}\d(?![0-9a-f])")

REDACTED = "[REDACTED]"


def _key_is_sensitive(key: str) -> bool:
    lowered = key.lower()
    if lowered.startswith(_SAFE_KEY_PREFIXES):
        return False
    return any(marker in lowered for marker in _SENSITIVE_KEYS)


def _scrub_string(value: str) -> str:
    value = _EMAIL.sub(REDACTED, value)
    return _PHONE.sub(REDACTED, value)


def _scrub(value: Any, key: str = "") -> Any:
    if key and _key_is_sensitive(key):
        return REDACTED
    if isinstance(value, str):
        return _scrub_string(value)
    if isinstance(value, dict):
        return {k: _scrub(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_scrub(v) for v in value]
    if hasattr(value, "model_dump"):
        # A pydantic model logged directly: scrub its dict form.
        return _scrub(value.model_dump(), key)
    return value


def redact_pii(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: scrub every value in the event dict."""
    for key in list(event_dict):
        event_dict[key] = _scrub(event_dict[key], key if key != "event" else "")
    return event_dict


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        redact_pii,
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # stdlib logging (third-party libraries) goes through the same
    # processors, so it is JSON and redacted too.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # httpx logs every request at INFO with the full URL; that is noise
    # next to our own structured line per upload.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)
