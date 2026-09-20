"""The single failure type for normalisation.

Design section 5: normalisation failures are rejections, not best-effort
passes. A digest of a badly-normalised value looks like a valid attempt and
silently drags down match rate, so we refuse to produce one.
"""


class NormalisationError(ValueError):
    """Raised when a value cannot be normalised to the platform's spec.

    Carries a field name and a short machine-readable reason code rather than
    a free-text message. Downstream code (the ingest API's rejection reason,
    the events_rejected_total metric label) keys off `reason`, and we do not
    want that logic parsing English sentences.

    The original raw value is deliberately NOT stored on the exception. This
    exception will end up in logs and error responses, and section 9 says no
    raw PII in either.
    """

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"{field}: {reason}")
