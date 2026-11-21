"""Unit tests for app/core/security.py and app/core/db_errors.py."""

from types import SimpleNamespace

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
