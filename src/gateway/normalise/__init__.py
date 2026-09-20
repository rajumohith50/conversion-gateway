"""Pure normalisation and hashing functions.

Nothing in this package does I/O, logging, or config reads. Every normaliser
takes a string (or None) and returns either the normalised string or a
RejectionReason. That is what makes the fixture tables in tests/normalise
the specification: if a platform changes its rules, the table changes and
the failing test tells you exactly which function to touch.
"""

from gateway.normalise.address import normalise_street
from gateway.normalise.email import normalise_email
from gateway.normalise.hashing import sha256_hex
from gateway.normalise.identifiers import (
    HashedIdentifiers,
    IdentifierBuildResult,
    RawIdentifiers,
    build_user_identifiers,
)
from gateway.normalise.name import normalise_name
from gateway.normalise.phone import normalise_phone
from gateway.normalise.rejection import FieldRejection, RejectionReason

__all__ = [
    "FieldRejection",
    "HashedIdentifiers",
    "IdentifierBuildResult",
    "RawIdentifiers",
    "RejectionReason",
    "build_user_identifiers",
    "normalise_email",
    "normalise_name",
    "normalise_phone",
    "normalise_street",
    "sha256_hex",
]
