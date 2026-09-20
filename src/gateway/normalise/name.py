"""Given / family name normalisation. Design section 5, "Given and family name"."""

import unicodedata

from gateway.normalise.errors import NormalisationError
from gateway.normalise.hashing import sha256_hex

# Honorifics and generational suffixes that CRMs routinely leave inside the
# name field ("Dr. Jane Smith", "Bob Jones Jr"). The platform's matching side
# stores bare names, so these tokens only reduce match rate.
#
# Kept as a module-level constant rather than config: this is part of the
# normalisation spec, and the fixture table in tests/normalise/test_name.py
# documents it. Deliberately short. Every entry here is a token we will
# silently delete from someone's name, so the bar for adding one is high.
# (E.g. "king" is a title but also a surname, so it is not in the list.)
_TITLES = frozenset({"mr", "mrs", "ms", "miss", "mx", "dr", "prof", "sir", "madam"})
_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "esq", "phd", "md"})
_STRIP_TOKENS = _TITLES | _SUFFIXES


def _strip_accents(value: str) -> str:
    """'José' -> 'Jose'. NFKD splits each accented char into base + combining
    mark; dropping the marks (category Mn) leaves the base letter. This also
    folds compatibility forms like 'ﬁ' into 'fi'."""
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def _strip_punctuation(value: str) -> str:
    """Remove punctuation entirely rather than replacing it with a space.

    "O'Brien" should become "obrien", not "o brien"; "Smith-Jones" becomes
    "smithjones". Both match how the platform side canonicalises. Whitespace is
    preserved here so that title/suffix tokens can still be found by splitting.
    """
    return "".join(ch for ch in value if not unicodedata.category(ch).startswith("P"))


def normalise_name(raw: str, field: str = "name") -> str:
    """Return the canonical form of a personal name component.

    `field` is only used to label the error ("given_name" vs "family_name")
    so the rejection reason names the right input.
    """
    value = _strip_punctuation(_strip_accents(raw.strip().lower()))
    tokens = [t for t in value.split() if t not in _STRIP_TOKENS]

    if not tokens:
        # Either the input was blank, or it was nothing but titles ("Dr.").
        raise NormalisationError(field, "empty")

    # Names are sent as a single token with internal whitespace removed, so
    # "mary ann" and "maryann" hash identically.
    return "".join(tokens)


def hash_name(raw: str, field: str = "name") -> str:
    return sha256_hex(normalise_name(raw, field))
