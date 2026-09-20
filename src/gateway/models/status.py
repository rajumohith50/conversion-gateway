"""Event lifecycle. Design section 4, plus REJECTED.

    RECEIVED -> VALIDATED -> {SUPPRESSED | QUEUED} -> UPLOADING
             -> {UPLOADED | FAILED_RETRYABLE | DEAD_LETTERED}
    RECEIVED -> REJECTED

REJECTED is not in the design's diagram. It is the terminal state for an
event that authenticated but failed schema validation (phase 2) or
normalisation (phase 3). The design says rejected events must be queryable,
which means they need a row, which means they need a state.
"""

from enum import StrEnum


class EventStatus(StrEnum):
    RECEIVED = "RECEIVED"
    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"
    SUPPRESSED = "SUPPRESSED"
    QUEUED = "QUEUED"
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
    EventStatus.VALIDATED: frozenset(
        {EventStatus.SUPPRESSED, EventStatus.QUEUED, EventStatus.REJECTED}
    ),
    EventStatus.SUPPRESSED: frozenset({EventStatus.QUEUED}),
    EventStatus.QUEUED: frozenset(
        {EventStatus.UPLOADING, EventStatus.SUPPRESSED, EventStatus.REJECTED}
    ),
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
