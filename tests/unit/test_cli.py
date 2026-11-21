"""Unit tests for the app.cli argument parser."""

import uuid

import pytest

from app.cli import _tenant_id, build_parser
from app.models.tenant import DEFAULT_TENANT_ID


def test_parser_commands() -> None:
    parser = build_parser()
    assert parser.parse_args(["create-tenant", "--name", "acme"]).name == "acme"
    tid = str(uuid.uuid4())
    assert parser.parse_args(["create-api-key", "--tenant-id", tid]).tenant_id == tid
    assert (
        parser.parse_args(["revoke-api-key", "--prefix", "ipps_x"]).prefix == "ipps_x"
    )


def test_parser_requires_a_command() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_tenant_id_accepts_default_alias() -> None:
    assert _tenant_id("default") == DEFAULT_TENANT_ID
    tid = uuid.uuid4()
    assert _tenant_id(str(tid)) == tid
    with pytest.raises(ValueError):
        _tenant_id("nope")
