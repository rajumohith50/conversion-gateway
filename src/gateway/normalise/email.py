"""Email normalisation. Design section 5, "Email"."""

from gateway.normalise.errors import NormalisationError
from gateway.normalise.hashing import sha256_hex

# Google-hosted mailboxes ignore dots in the local part and everything after
# a "+". "m.ohith+ads@gmail.com" and "mohith@gmail.com" are the same inbox, so
# they must hash to the same digest or the match silently fails.
# Other providers do not have this rule, so we must NOT apply it to them:
# "a.b@outlook.com" and "ab@outlook.com" are different people.
_DOT_INSENSITIVE_DOMAINS = frozenset({"gmail.com", "googlemail.com"})


def normalise_email(raw: str) -> str:
    """Return the canonical form of an email address.

    Raises NormalisationError if the input is not shaped like an address.
    We only check for exactly one "@" with non-empty sides; full RFC 5322
    validation would reject real addresses that platforms happily match on.
    """
    value = raw.strip().lower()
    if not value:
        raise NormalisationError("email", "empty")

    local, sep, domain = value.partition("@")
    if not sep or not local or not domain or "@" in domain:
        raise NormalisationError("email", "malformed")

    if domain in _DOT_INSENSITIVE_DOMAINS:
        local = local.replace(".", "")
        local = local.split("+", 1)[0]
        if not local:
            # "+tag@gmail.com" or "...@gmail.com": nothing left to match on.
            raise NormalisationError("email", "malformed")

    return f"{local}@{domain}"


def hash_email(raw: str) -> str:
    return sha256_hex(normalise_email(raw))
