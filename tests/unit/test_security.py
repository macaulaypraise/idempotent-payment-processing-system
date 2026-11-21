"""Unit tests for app/core/security.py and app/core/db_errors.py."""

from types import SimpleNamespace
from typing import Any

import pytest

from app.core.db_errors import pg_constraint_name, pg_sqlstate, violates_constraint
from app.core.security import (
    API_KEY_PREFIX,
    constant_time_equals,
    generate_api_key,
    hash_api_key,
    parse_bearer_token,
)


def test_generated_key_hash_and_prefix_are_consistent() -> None:
    plaintext, prefix, key_hash = generate_api_key()
    assert plaintext.startswith(API_KEY_PREFIX)
    assert len(plaintext) > 40
    assert plaintext.startswith(prefix) and len(prefix) == 12
    assert key_hash == hash_api_key(plaintext)
    assert len(key_hash) == 64
    assert plaintext not in key_hash


def test_generated_keys_are_unique() -> None:
    assert len({generate_api_key()[0] for _ in range(50)}) == 50


@pytest.mark.parametrize(
    "header,expected",
    [
        ("Bearer abc", "abc"),
        ("bearer   abc  ", "abc"),
        ("Basic abc", None),
        ("Bearer ", None),
        ("abc", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_bearer_token(header: str | None, expected: str | None) -> None:
    assert parse_bearer_token(header) == expected


def test_constant_time_equals() -> None:
    assert constant_time_equals("a", "a")
    assert not constant_time_equals("a", "b")


def _wrapped(**attrs: object) -> Exception:
    """Mimic SQLAlchemy DBAPIError -> adapted error -> asyncpg error chain."""
    driver = Exception("driver")
    for name, value in attrs.items():
        setattr(driver, name, value)
    adapted = Exception("adapted")
    adapted.__cause__ = driver
    outer = Exception("outer")
    outer.orig = adapted  # type: ignore[attr-defined]
    return outer


def test_sqlstate_found_on_driver_exception() -> None:
    assert pg_sqlstate(_wrapped(sqlstate="55P03")) == "55P03"


def test_sqlstate_found_as_pgcode() -> None:
    exc = Exception("x")
    exc.orig = SimpleNamespace(pgcode="23505")  # type: ignore[attr-defined]
    assert pg_sqlstate(exc) == "23505"


def test_sqlstate_missing() -> None:
    assert pg_sqlstate(Exception("plain")) is None


def test_constraint_name_lookup_and_fallback() -> None:
    exc = _wrapped(constraint_name="uq_x")
    assert pg_constraint_name(exc) == "uq_x"
    assert violates_constraint(exc, "uq_x")
    assert not violates_constraint(exc, "uq_y")
    assert violates_constraint(Exception('violates "uq_z"'), "uq_z")
    assert pg_constraint_name(Exception("plain")) is None


# --- webhook signatures ------------------------------------------------------------

SECRET = "whsec_test"
BODY = b'{"id":"evt_1","type":"charge.succeeded","data":{}}'


def test_signature_format_matches_an_independent_computation() -> None:
    """Pinned against hmac directly, so a bug shared by the signer (used by
    the simulator) and the verifier cannot pass unnoticed."""
    import hashlib
    import hmac as std_hmac

    from app.core.security import sign_webhook

    expected = std_hmac.new(
        SECRET.encode(), b"1700000000." + BODY, hashlib.sha256
    ).hexdigest()
    assert sign_webhook(SECRET, BODY, 1_700_000_000) == f"t=1700000000,v1={expected}"


def test_valid_signature_verifies_and_returns_the_timestamp() -> None:
    from app.core.security import sign_webhook, verify_webhook

    header = sign_webhook(SECRET, BODY, 1_000)
    assert (
        verify_webhook(SECRET, BODY, header, now=1_010, tolerance_seconds=300) == 1_000
    )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda h, b: (h, b + b" "), "mismatch"),  # body tampered
        (lambda h, b: (h.replace("t=1000", "t=1001"), b), "mismatch"),  # ts edited
        (lambda h, b: (h[:-1] + ("0" if h[-1] != "0" else "1"), b), "mismatch"),
        (lambda h, b: ("v1=abc", b), "needs t= and v1="),
        (lambda h, b: ("t=abc,v1=abc", b), "malformed signature timestamp"),
        (lambda h, b: ("garbage", b), "malformed signature header"),
        (lambda h, b: (None, b), "missing"),
        (lambda h, b: ("", b), "missing"),
    ],
)
def test_bad_signatures_are_rejected(mutate: Any, message: str) -> None:
    from app.core.security import SignatureError, sign_webhook, verify_webhook

    header, body = mutate(sign_webhook(SECRET, BODY, 1_000), BODY)
    with pytest.raises(SignatureError, match=message):
        verify_webhook(SECRET, body, header, now=1_000, tolerance_seconds=300)


@pytest.mark.parametrize("now", [1_000 + 301, 1_000 - 301])  # stale, and future
def test_timestamps_outside_tolerance_are_rejected(now: int) -> None:
    from app.core.security import SignatureError, sign_webhook, verify_webhook

    header = sign_webhook(SECRET, BODY, 1_000)
    with pytest.raises(SignatureError, match="tolerance"):
        verify_webhook(SECRET, BODY, header, now=now, tolerance_seconds=300)


def test_wrong_secret_and_empty_secret_are_rejected() -> None:
    from app.core.security import SignatureError, sign_webhook, verify_webhook

    header = sign_webhook(SECRET, BODY, 1_000)
    with pytest.raises(SignatureError, match="mismatch"):
        verify_webhook("other", BODY, header, now=1_000, tolerance_seconds=300)
    with pytest.raises(SignatureError, match="not configured"):
        verify_webhook("", BODY, header, now=1_000, tolerance_seconds=300)


def test_any_of_several_v1_values_is_accepted_for_secret_rotation() -> None:
    from app.core.security import sign_webhook, verify_webhook

    new = sign_webhook(SECRET, BODY, 1_000)
    old = sign_webhook("old_secret", BODY, 1_000).split(",")[1]
    header = f"{new.split(',')[0]},{old},{new.split(',')[1]}"
    assert verify_webhook(SECRET, BODY, header, now=1_000, tolerance_seconds=300)
