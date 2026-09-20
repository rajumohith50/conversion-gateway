"""Event lifecycle. Design section 4, plus REJECTED and PROCESSED.

    RECEIVED -> VALIDATED -> QUEUED -> {SUPPRESSED | REJECTED | PROCESSED}
    PROCESSED -> UPLOADING -> {UPLOADED | FAILED_RETRYABLE | DEAD_LETTERED}
    RECEIVED -> REJECTED

Two states are not in the design's diagram:

REJECTED   terminal, for an event that authenticated but failed schema
           validation (phase 2) or normalisation (phase 3). The design says
           rejected events must be queryable, which means a row and a state.

PROCESSED  the worker has gated consent, hashed the identifiers and written
           the digests to the ledger; the event is waiting for the upload
           stage (phase 4). From here on the ledger row is self-sufficient:
           the raw identifiers are gone and the digests are durable, so a
           crash between hashing and upload loses nothing.
"""

from enum import StrEnum


class EventStatus(StrEnum):
    RECEIVED = "RECEIVED"
    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"
    QUEUED = "QUEUED"
    SUPPRESSED = "SUPPRESSED"
    PROCESSED = "PROCESSED"
    UPLOADING = "UPLOADING"
    UPLOADED = "UPLOADED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    DEAD_LETTERED = "DEAD_LETTERED"


# The state machine as data. transition() in gateway.ledger refuses anything
# not listed here, so an impossible transition (UPLOADED -> RECEIVED) is a
# bug caught at the call site rather than a corrupt ledger row.
#
# SUPPRESSED and DEAD_LETTERED are terminal for the pipeline but replayable
# by an operator (section 6, section 7), so they may go back to QUEUED.
# REJECTED and UPLOADED are truly terminal.
ALLOWED_TRANSITIONS: dict[EventStatus, frozenset[EventStatus]] = {
    EventStatus.RECEIVED: frozenset({EventStatus.VALIDATED, EventStatus.REJECTED}),
    EventStatus.VALIDATED: frozenset({EventStatus.QUEUED}),
    EventStatus.QUEUED: frozenset(
        {EventStatus.PROCESSED, EventStatus.SUPPRESSED, EventStatus.REJECTED}
    ),
    EventStatus.SUPPRESSED: frozenset({EventStatus.QUEUED}),
    EventStatus.PROCESSED: frozenset({EventStatus.UPLOADING}),
    EventStatus.UPLOADING: frozenset(
        {EventStatus.UPLOADED, EventStatus.FAILED_RETRYABLE, EventStatus.DEAD_LETTERED}
    ),
    EventStatus.FAILED_RETRYABLE: frozenset({EventStatus.UPLOADING, EventStatus.DEAD_LETTERED}),
    EventStatus.DEAD_LETTERED: frozenset({EventStatus.QUEUED}),
    EventStatus.UPLOADED: frozenset(),
    EventStatus.REJECTED: frozenset(),
}


class Source(StrEnum):
    SALESFORCE = "salesforce"
    HUBSPOT = "hubspot"


class MatchKeyType(StrEnum):
    CLICK_ID = "click_id"
    USER_IDENTIFIERS = "user_identifiers"
