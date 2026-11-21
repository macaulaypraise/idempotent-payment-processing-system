"""API keys and provider webhook signatures.

API keys are 32 random bytes, so a fast hash (SHA-256) is appropriate: there
is no low-entropy secret to protect against brute force, unlike a password.
Only the hash is stored; the plaintext is returned once at creation.

Webhook signatures are HMAC-SHA256 over "{timestamp}.{raw body}", sent as
`PSP-Signature: t=<unix seconds>,v1=<hex>`. The timestamp is signed, so a
captured request cannot be replayed outside the tolerance window by editing
it. The PSP simulator imports sign_webhook from here; the unit tests pin the
format with a vector computed independently, so a shared bug cannot hide.
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


# --- provider webhooks -----------------------------------------------------------

SIGNATURE_HEADER = "PSP-Signature"


class SignatureError(Exception):
    """The webhook is not authentic or is outside the tolerance window."""


def _webhook_mac(secret: str, timestamp: int, body: bytes) -> str:
    message = str(timestamp).encode() + b"." + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def sign_webhook(secret: str, body: bytes, timestamp: int) -> str:
    """Return the PSP-Signature header value for `body` sent at `timestamp`."""
    return f"t={timestamp},v1={_webhook_mac(secret, timestamp, body)}"


def verify_webhook(
    secret: str,
    body: bytes,
    header: str | None,
    *,
    now: float,
    tolerance_seconds: float,
) -> int:
    """Check a PSP-Signature header against the raw body; return its timestamp.

    Several v1 values are accepted so the provider can rotate secrets. The
    MAC is checked before the timestamp and compared in constant time. An
    empty secret fails closed rather than accepting everything.
    """
    if not secret:
        raise SignatureError("webhook secret is not configured")
    if not header:
        raise SignatureError(f"missing {SIGNATURE_HEADER} header")
    timestamp: int | None = None
    candidates: list[str] = []
    for part in header.split(","):
        key, sep, value = part.strip().partition("=")
        if not sep:
            raise SignatureError("malformed signature header")
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError as exc:
                raise SignatureError("malformed signature timestamp") from exc
        elif key == "v1":
            candidates.append(value)
    if timestamp is None or not candidates:
        raise SignatureError("signature header needs t= and v1=")
    expected = _webhook_mac(secret, timestamp, body)
    if not any(hmac.compare_digest(expected, c) for c in candidates):
        raise SignatureError("signature mismatch")
    if abs(now - timestamp) > tolerance_seconds:
        raise SignatureError("timestamp outside tolerance")
    return timestamp
