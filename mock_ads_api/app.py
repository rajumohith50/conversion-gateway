"""Mock offline conversion upload API.

    POST /v1/conversions:upload   the upload endpoint
    GET/POST/DELETE /control      inspect, set, reset failure injection
    GET/DELETE /uploads           what has been accepted so far

Request and response mirror Google Ads' UploadClickConversions: a
`conversions` array, `partialFailure: true`, and a response with one
`results` entry per input row plus a `partialFailureError` whose per-error
`location.fieldPathElements[].index` says which row failed. Error codes
are drawn from the real ConversionUploadError enum. The path and auth
differ from the real service; the shapes do not.

The mock is strict about hashed identifiers on purpose: a digest that is
not 64 lowercase hex characters is a row error. That catches the gateway's
own hashing bugs (wrong casing, hashing the wrong string, hex of the wrong
length) before a real account ever sees them.
"""

import asyncio
import os
import re
from typing import Any, Literal

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

HEX64 = re.compile(r"^[0-9a-f]{64}$")
CONVERSION_ACTION = re.compile(r"^customers/\d+/conversionActions/[A-Za-z0-9_]+$")
# "yyyy-mm-dd hh:mm:ss+hh:mm", the format the real API requires.
DATE_TIME = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")

Mode = Literal["ok", "http_401", "http_429", "http_500", "http_503", "timeout", "malformed"]


class RowFailure(BaseModel):
    index: int = Field(ge=0)
    error_code: str = "INVALID_CONVERSION_ACTION"


class Control(BaseModel):
    """Failure injection. `times` is how many upcoming requests the setting
    applies to; None means until reset. After `times` requests the mock
    reverts to plain `ok`."""

    model_config = ConfigDict(extra="forbid")

    mode: Mode = "ok"
    fail_rows: list[RowFailure] = Field(default_factory=list)
    times: int | None = None
    timeout_seconds: float = 3.0


class State:
    def __init__(self, known_actions: set[str]) -> None:
        self.known_actions = known_actions
        self.control = Control()
        self.uploads: list[dict[str, Any]] = []
        self.request_count = 0

    def take_control(self) -> Control:
        """The control that applies to this request, consuming one `times`."""
        current = self.control
        if current.times is not None:
            if current.times <= 0:
                self.control = Control()
                return self.control
            self.control = current.model_copy(update={"times": current.times - 1})
        return current


def _action_name(resource: str) -> str:
    return resource.rsplit("/", 1)[-1]


def validate_row(row: Any, known_actions: set[str]) -> str | None:
    """Return an error code for the row, or None if it is acceptable."""
    if not isinstance(row, dict):
        return "INVALID_CONVERSION"
    action = row.get("conversionAction")
    if not isinstance(action, str) or not CONVERSION_ACTION.match(action):
        return "INVALID_CONVERSION_ACTION"
    if _action_name(action) not in known_actions:
        return "CONVERSION_ACTION_NOT_FOUND"
    when = row.get("conversionDateTime")
    if not isinstance(when, str) or not DATE_TIME.match(when):
        return "INVALID_CONVERSION_DATE_TIME"

    gclid = row.get("gclid")
    identifiers = row.get("userIdentifiers") or []
    if not gclid and not identifiers:
        return "USER_IDENTIFIER_REQUIRED"

    for ident in identifiers:
        if not isinstance(ident, dict):
            return "INVALID_USER_IDENTIFIER"
        for key in ("hashedEmail", "hashedPhoneNumber"):
            if key in ident and not HEX64.match(str(ident[key])):
                return "INVALID_USER_IDENTIFIER"
        address = ident.get("addressInfo")
        if address is not None:
            for key in ("hashedFirstName", "hashedLastName", "hashedStreetAddress"):
                if key in address and not HEX64.match(str(address[key])):
                    return "INVALID_USER_IDENTIFIER"
    if identifiers:
        consent = row.get("consent") or {}
        if consent.get("adUserData") != "GRANTED":
            return "CONSENT_REQUIRED_FOR_USER_IDENTIFIERS"
    return None


def create_app(known_actions: set[str] | None = None, initial_mode: Mode = "ok") -> FastAPI:
    state = State(known_actions or {"closed_won", "qualified_lead"})
    state.control = Control(mode=initial_mode)
    app = FastAPI(title="Mock Ads API")
    app.state.mock = state

    @app.post("/v1/conversions:upload")
    async def upload(request: Request) -> Response:
        state.request_count += 1
        control = state.take_control()

        if control.mode == "timeout":
            await asyncio.sleep(control.timeout_seconds)
        if control.mode.startswith("http_"):
            http_code = int(control.mode.removeprefix("http_"))
            status_name = {401: "UNAUTHENTICATED", 429: "RESOURCE_EXHAUSTED"}.get(
                http_code, "INTERNAL"
            )
            return JSONResponse(
                {
                    "error": {
                        "code": http_code,
                        "message": f"injected {http_code}",
                        "status": status_name,
                    }
                },
                status_code=http_code,
            )
        if control.mode == "malformed":
            return PlainTextResponse("<html>this is not json</html>", status_code=200)

        body = await request.json()
        conversions = body.get("conversions")
        if not isinstance(conversions, list):
            return JSONResponse(
                {
                    "error": {
                        "code": 400,
                        "message": "conversions must be a list",
                        "status": "INVALID_ARGUMENT",
                    }
                },
                status_code=400,
            )
        injected = {f.index: f.error_code for f in control.fail_rows}

        results: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for index, row in enumerate(conversions):
            code = injected.get(index) or validate_row(row, state.known_actions)
            if code is not None:
                results.append({})
                errors.append(
                    {
                        "errorCode": {"conversionUploadError": code},
                        "message": f"row {index}: {code}",
                        "location": {
                            "fieldPathElements": [{"fieldName": "conversions", "index": index}]
                        },
                    }
                )
                continue
            state.uploads.append(row)
            results.append(
                {
                    "gclid": row.get("gclid"),
                    "conversionAction": row["conversionAction"],
                    "conversionDateTime": row["conversionDateTime"],
                    "orderId": row.get("orderId"),
                }
            )

        response: dict[str, Any] = {"results": results}
        if errors:
            response["partialFailureError"] = {
                "code": 3,
                "message": f"{len(errors)} of {len(conversions)} conversions failed",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.ads.googleads.errors.GoogleAdsFailure",
                        "errors": errors,
                    }
                ],
            }
        return JSONResponse(response)

    @app.get("/control")
    def get_control() -> Control:
        return state.control

    @app.post("/control")
    def set_control(control: Control) -> Control:
        state.control = control
        return control

    @app.delete("/control")
    def reset_control() -> Control:
        state.control = Control()
        return state.control

    @app.get("/uploads")
    def list_uploads() -> dict[str, Any]:
        return {
            "count": len(state.uploads),
            "uploads": state.uploads,
            "requests": state.request_count,
        }

    @app.delete("/uploads")
    def clear_uploads() -> dict[str, int]:
        state.uploads.clear()
        state.request_count = 0
        return {"count": 0}

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


def create_app_from_env() -> FastAPI:
    actions = set(
        filter(
            None,
            os.environ.get("MOCK_KNOWN_CONVERSION_ACTIONS", "closed_won,qualified_lead").split(","),
        )
    )
    mode = os.environ.get("MOCK_MODE", "ok")
    return create_app(actions, mode)  # type: ignore[arg-type]
