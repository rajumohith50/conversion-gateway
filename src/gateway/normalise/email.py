"""Email normalisation. Design section 5, "Email"."""

from gateway.normalise.rejection import RejectionReason

# Google-hosted mailboxes ignore dots in the local part and everything after
# a "+". "m.ohith+ads@gmail.com" and "mohith@gmail.com" are the same inbox, so
# they must hash to the same digest or the match silently fails.
# Other providers do not have this rule, so we must NOT apply it to them:
# "a.b@outlook.com" and "ab@outlook.com" are different people.
_DOT_INSENSITIVE_DOMAINS = frozenset({"gmail.com", "googlemail.com"})


def normalise_email(raw: str | None) -> str | RejectionReason:
    """Return the canonical form of an email address, or why it was rejected.

    We only check for exactly one "@" with non-empty sides; full RFC 5322
    validation would reject real addresses that platforms happily match on.
    """
    if raw is None:
        return RejectionReason.MISSING

    value = raw.strip().lower()
    if not value:
        return RejectionReason.EMPTY

    local, sep, domain = value.partition("@")
    if not sep or not local or not domain or "@" in domain:
        return RejectionReason.MALFORMED

    if domain in _DOT_INSENSITIVE_DOMAINS:
        local = local.replace(".", "")
        local = local.split("+", 1)[0]
        if not local:
            # "+tag@gmail.com" or "...@gmail.com": nothing left to match on.
            return RejectionReason.MALFORMED

    return f"{local}@{domain}"
