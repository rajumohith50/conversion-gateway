"""One place that decides how a normalised string becomes a digest."""

import hashlib


def sha256_hex(value: str) -> str:
    """SHA-256 of the UTF-8 encoding, as lowercase hex.

    Centralised so the encoding choice is made exactly once. If two call sites
    encoded differently (say one used UTF-16), the digests would silently
    disagree with the platform's and nothing would match.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
