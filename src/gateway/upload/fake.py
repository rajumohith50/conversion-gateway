"""Scripted UploadClient for tests of everything above the HTTP layer.

Each call pops the next scripted outcome: a BatchResult to return, or an
exception to raise. Every batch received is recorded so a test can assert
exactly which conversions were sent, and how many times.
"""

from collections import deque
from typing import Any

from gateway.upload.client import BatchResult, RowResult, UploadClient


class FakeUploadClient(UploadClient):
    def __init__(self, script: list[BatchResult | Exception] | None = None) -> None:
        self.script: deque[BatchResult | Exception] = deque(script or [])
        self.calls: list[list[dict[str, Any]]] = []

    def upload(self, conversions: list[dict[str, Any]]) -> BatchResult:
        self.calls.append(list(conversions))
        if not self.script:
            return all_ok(len(conversions))
        outcome = self.script.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def sent_order_ids(self) -> list[list[str]]:
        return [[row["orderId"] for row in call] for call in self.calls]


def all_ok(n: int) -> BatchResult:
    return BatchResult(rows=[RowResult(index=i, ok=True) for i in range(n)])


def with_failures(n: int, failures: dict[int, str]) -> BatchResult:
    return BatchResult(
        rows=[
            RowResult(index=i, ok=False, error_code=failures[i], message=f"row {i}")
            if i in failures
            else RowResult(index=i, ok=True)
            for i in range(n)
        ]
    )
