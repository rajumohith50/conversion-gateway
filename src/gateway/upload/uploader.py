"""The upload stage: ledger rows in, platform calls out, outcomes back.

Driven from the ledger rather than the queue. Once a row is PROCESSED it
holds everything the upload needs, so claiming from the ledger means a
crash anywhere in this file loses nothing: the row stays UPLOADING and is
re-claimed as stale.

Per batch:
  1. claim()            PROCESSED / due FAILED_RETRYABLE / stale UPLOADING
                        rows -> UPLOADING, attempt_count += 1
  2. batcher            wait for a full batch or the time limit
  3. send_with_retry    tenacity around client.upload(); between attempts
                        the ledger records FAILED_RETRYABLE -> UPLOADING
  4. apply_result       per row: UPLOADED, FAILED_RETRYABLE, DEAD_LETTERED

Partial failures are handled per row, by index (step 4). The batch is
never re-sent because some rows failed: the rows that succeeded are
already UPLOADED and would be double-counted.
"""

import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from gateway import ledger
from gateway.models.ledger import Event
from gateway.models.status import EventStatus
from gateway.upload.backoff import RetryPolicy, call_with_retry
from gateway.upload.batcher import Batcher
from gateway.upload.classifier import FailureClass, classify_row_error
from gateway.upload.client import BatchResult, UploadClient
from gateway.upload.conversion import build_conversion
from gateway.upload.errors import PermanentUploadError, PoisonUploadError, TransientUploadError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Claimed:
    """A row we own for this upload cycle. The conversion dict is built at
    claim time, inside the transaction, so the batch never needs the ORM
    object again."""

    event_id: str
    attempt_count: int
    conversion: dict[str, Any]


@dataclass(frozen=True)
class CycleReport:
    uploaded: int = 0
    retry_later: int = 0
    dead_lettered: int = 0


class Uploader:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        client: UploadClient,
        policy: RetryPolicy,
        *,
        customer_id: str,
        batch_size: int,
        batch_wait_seconds: float,
        row_retry_after: timedelta,
        stale_after: timedelta,
        max_row_attempts: int,
    ) -> None:
        self._session_factory = session_factory
        self._client = client
        self._policy = policy
        self._customer_id = customer_id
        self._batcher: Batcher[Claimed] = Batcher(batch_size, batch_wait_seconds)
        self._row_retry_after = row_retry_after
        self._stale_after = stale_after
        self._max_row_attempts = max_row_attempts

    # --- step 1 ----------------------------------------------------------------

    def claim(self, now: datetime, limit: int) -> list[Claimed]:
        with self._session_factory.begin() as session:
            rows = ledger.claim_for_upload(
                session, now, limit, self._row_retry_after, self._stale_after
            )
            claimed = []
            for event in rows:
                # A row that has already been attempted the maximum number
                # of times is poison even though each attempt was
                # individually retryable. Dead-letter it here rather than
                # spend another request on it.
                if event.attempt_count > self._max_row_attempts:
                    ledger.transition(
                        session,
                        event,
                        EventStatus.DEAD_LETTERED,
                        f"poison:row_attempt_ceiling:{event.attempt_count - 1}",
                        now,
                    )
                    continue
                claimed.append(
                    Claimed(
                        event_id=event.event_id,
                        attempt_count=event.attempt_count,
                        conversion=build_conversion(event, self._customer_id),
                    )
                )
            return claimed

    # --- steps 2-4 -------------------------------------------------------------

    def run_once(self, now: datetime) -> CycleReport:
        """One cycle: claim what is ready, batch, upload if a batch is due."""
        room = self._batcher.max_size - len(self._batcher)
        batch: list[Claimed] | None = None
        if room > 0:
            for item in self.claim(now, room):
                full = self._batcher.add(item, now)
                if full is not None:
                    batch = full
        if batch is None:
            batch = self._batcher.flush_if_due(now)
        if not batch:
            return CycleReport()
        return self.upload_batch(batch, now)

    def upload_batch(self, batch: list[Claimed], now: datetime) -> CycleReport:
        event_ids = [c.event_id for c in batch]
        conversions = [c.conversion for c in batch]

        def on_retry(exc: TransientUploadError, attempt: int) -> None:
            # Every attempt is visible in the ledger, with its reason.
            with self._session_factory.begin() as session:
                for event in _load(session, event_ids):
                    ledger.record_retry_attempt(
                        session, event, f"transient:{exc.reason}", datetime.now(UTC)
                    )
            log.warning("upload attempt %d failed (%s); retrying", attempt, exc.reason)

        try:
            result = call_with_retry(
                self._policy, lambda: self._client.upload(conversions), on_retry
            )
        except PermanentUploadError as exc:
            return self._dead_letter_all(event_ids, f"permanent:{exc.reason}", now)
        except PoisonUploadError as exc:
            return self._dead_letter_all(
                event_ids, f"poison:{exc.reason}:after_{exc.attempts}_attempts", now
            )

        return self.apply_result(batch, result, now)

    def apply_result(self, batch: list[Claimed], result: BatchResult, now: datetime) -> CycleReport:
        """Per-row outcomes, mapped by index. This is the function the
        design calls out: act per row, never re-send the batch."""
        uploaded = retry_later = dead = 0
        by_id = {c.event_id: c for c in batch}
        with self._session_factory.begin() as session:
            events = {e.event_id: e for e in _load(session, list(by_id))}
            for claimed, row in zip(batch, result.rows, strict=True):
                event = events[claimed.event_id]
                if row.ok:
                    ledger.transition(session, event, EventStatus.UPLOADED, None, now)
                    uploaded += 1
                    continue
                code = row.error_code or "UNKNOWN"
                if classify_row_error(code) is FailureClass.TRANSIENT:
                    # Row goes back to the pool; claim() picks it up after
                    # row_retry_after, up to the attempt ceiling.
                    ledger.transition(
                        session, event, EventStatus.FAILED_RETRYABLE, f"retryable_row:{code}", now
                    )
                    retry_later += 1
                else:
                    ledger.transition(
                        session, event, EventStatus.DEAD_LETTERED, f"permanent_row:{code}", now
                    )
                    dead += 1
        log.info(
            "batch done: %d uploaded, %d retry later, %d dead-lettered",
            uploaded,
            retry_later,
            dead,
        )
        return CycleReport(uploaded=uploaded, retry_later=retry_later, dead_lettered=dead)

    def _dead_letter_all(self, event_ids: list[str], reason: str, now: datetime) -> CycleReport:
        with self._session_factory.begin() as session:
            for event in _load(session, event_ids):
                ledger.transition(session, event, EventStatus.DEAD_LETTERED, reason, now)
        log.error("batch of %d dead-lettered: %s", len(event_ids), reason)
        return CycleReport(dead_lettered=len(event_ids))


def _load(session: Session, event_ids: list[str]) -> list[Event]:
    events = [ledger.get_event(session, event_id) for event_id in event_ids]
    return [e for e in events if e is not None]


def run(uploader: Uploader, stop: threading.Event, poll_interval_seconds: float) -> None:
    log.info("uploader started")
    while not stop.is_set():
        report = uploader.run_once(datetime.now(UTC))
        if report == CycleReport():
            # Nothing happened; do not spin on the database.
            stop.wait(poll_interval_seconds)
    log.info("uploader stopped")
