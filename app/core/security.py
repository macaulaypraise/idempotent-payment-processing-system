"""API key generation and hashing.

Keys are 32 random bytes, so a fast hash (SHA-256) is appropriate: there is
no low-entropy secret to protect against brute force, unlike a password.
Only the hash is stored; the plaintext is returned once at creation.
"""

import hashlib
import hmac
import secrets

API_KEY_PREFIX = "ipps_"
_DISPLAY_PREFIX_LENGTH = 12


def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """Return (plaintext, display_prefix, sha256_hex)."""
    plaintext = API_KEY_PREFIX + secrets.token_urlsafe(32)
    return plaintext, plaintext[:_DISPLAY_PREFIX_LENGTH], hash_api_key(plaintext)


def parse_bearer_token(authorization: str | None) -> str | None:
    """Extract the token from an `Authorization: Bearer <token>` header."""
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
