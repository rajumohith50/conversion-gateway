"""The upload client interface and its HTTP implementation.

UploadClient.upload() takes a list of conversion rows and returns a
BatchResult with one RowResult per input row, in input order. Whole-request
failures are raised as classified UploadErrors. That is the entire
contract; the batcher and the uploader never see HTTP.

HttpUploadClient speaks the Google Ads-shaped API. Pointed at the mock
locally, it needs a different base URL, path and auth to talk to the real
service, and nothing else.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx

from gateway.upload.classifier import FailureClass, classify_http_status
from gateway.upload.errors import PermanentUploadError, TransientUploadError


@dataclass(frozen=True)
class RowResult:
    index: int
    ok: bool
    error_code: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class BatchResult:
    rows: list[RowResult]

    @property
    def failed(self) -> list[RowResult]:
        return [r for r in self.rows if not r.ok]


class UploadClient(ABC):
    @abstractmethod
    def upload(self, conversions: list[dict[str, Any]]) -> BatchResult:
        """Upload one batch. Returns per-row results in input order.
        Raises TransientUploadError or PermanentUploadError for failures
        of the request as a whole."""


class HttpUploadClient(UploadClient):
    UPLOAD_PATH = "/v1/conversions:upload"

    def __init__(self, base_url: str, token: str, timeout_seconds: float) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout_seconds,
            headers={"Authorization": f"Bearer {token}"} if token else {},
        )

    def upload(self, conversions: list[dict[str, Any]]) -> BatchResult:
        try:
            response = self._client.post(
                self.UPLOAD_PATH, json={"conversions": conversions, "partialFailure": True}
            )
        except httpx.TimeoutException as exc:
            raise TransientUploadError("timeout") from exc
        except httpx.TransportError as exc:
            raise TransientUploadError(f"connection:{type(exc).__name__}") from exc

        if response.status_code != 200:
            reason = f"http_{response.status_code}"
            if classify_http_status(response.status_code) is FailureClass.TRANSIENT:
                raise TransientUploadError(reason)
            raise PermanentUploadError(reason)

        try:
            body = response.json()
            return parse_batch_response(body, expected_rows=len(conversions))
        except (ValueError, KeyError, TypeError) as exc:
            # We cannot tell which rows were accepted. Treat as transient:
            # the retry re-sends the whole batch, and orderId on every row
            # lets the platform drop any that it already has.
            raise TransientUploadError("malformed_response") from exc

    def close(self) -> None:
        self._client.close()


def parse_batch_response(body: Any, expected_rows: int) -> BatchResult:
    """Map the response back onto input rows by index.

    `results` has one entry per input row; an empty object marks a failure.
    The error detail for that row is found by its
    location.fieldPathElements[].index. Both must agree, and the count must
    match what we sent, or the response is malformed.
    """
    results = body["results"]
    if not isinstance(results, list) or len(results) != expected_rows:
        raise ValueError(f"expected {expected_rows} results, got {results!r}")

    errors_by_index: dict[int, tuple[str, str]] = {}
    for detail in (body.get("partialFailureError") or {}).get("details", []):
        for err in detail.get("errors", []):
            index = _row_index(err)
            if index is None:
                continue
            code = next(iter(err.get("errorCode", {}).values()), "UNKNOWN")
            errors_by_index[index] = (str(code), str(err.get("message", "")))

    rows: list[RowResult] = []
    for index, result in enumerate(results):
        if result:
            rows.append(RowResult(index=index, ok=True))
        else:
            code, message = errors_by_index.get(index, ("UNKNOWN", "no error detail for row"))
            rows.append(RowResult(index=index, ok=False, error_code=code, message=message))
    return BatchResult(rows=rows)


def _row_index(err: dict[str, Any]) -> int | None:
    for element in (err.get("location") or {}).get("fieldPathElements", []):
        if element.get("fieldName") == "conversions" and isinstance(element.get("index"), int):
            return int(element["index"])
    return None
