from gateway.normalise.hashing import sha256_hex


def test_known_answer() -> None:
    # Published SHA-256 of the empty string. Pins the algorithm and hex casing
    # against an external truth rather than our own output.
    assert sha256_hex("") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_utf8_encoding() -> None:
    # "é" is 2 bytes in UTF-8 (c3 a9) but 1 byte in latin-1 (e9). If the
    # encoding ever changed, this digest would change with it.
    assert sha256_hex("é") == "4a99557e4033c3539de2eb65472017cad5f9557f7a0625a09f1c3f6e2ba69c4c"


def test_lowercase_hex() -> None:
    digest = sha256_hex("anything")
    assert digest == digest.lower()
    assert len(digest) == 64
