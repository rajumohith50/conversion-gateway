"""Pure normalisation and hashing functions.

Nothing in this package does I/O. Every function takes a string in and returns
a string (or raises NormalisationError). That is what makes the fixture tables
in tests/normalise the specification: if a platform changes its rules, the
table changes and the failing test tells you exactly which function to touch.
"""

from gateway.normalise.errors import NormalisationError
from gateway.normalise.identifiers import (
    HashedIdentifiers,
    RawIdentifiers,
    normalise_identifiers,
)

__all__ = [
    "HashedIdentifiers",
    "NormalisationError",
    "RawIdentifiers",
    "normalise_identifiers",
]
