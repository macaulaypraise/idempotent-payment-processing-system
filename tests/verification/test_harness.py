"""The verification harness as tests.

test_state_machine runs the profile named by VERIFY_PROFILE: "pr" (default,
short and derandomized) in every CI run, "nightly" in the scheduled job.
test_harness_catches_a_planted_bug is the harness's own test: with the
charger sending a fresh provider key on every retry, it must find a double
charge. A harness that cannot find a planted bug proves nothing.
"""

import os
from collections.abc import Iterator

import pytest
from hypothesis import settings
from hypothesis.stateful import run_state_machine_as_test

from app.core.planted_bugs import plant
from tests.conftest import TEST_REDIS_URL
from verification import machine


@pytest.fixture
def harness(
    migrated_database: str, owner_database_url: str
) -> Iterator[machine.Harness]:
    configured = machine.configure(
        owner_database_url, migrated_database, TEST_REDIS_URL
    )
    try:
        yield configured
    finally:
        print(
            f"\nharness: {configured.examples} histories, {configured.steps} steps "
            f"checked (profile {os.environ.get('VERIFY_PROFILE', 'pr')})"
        )
        for name, count in sorted(configured.stats.items()):
            print(f"  {name}: {count}")
        machine.shutdown()


def test_state_machine(harness: machine.Harness) -> None:
    profile = settings.get_profile(os.environ.get("VERIFY_PROFILE", "pr"))
    run_state_machine_as_test(machine.LedgerlineMachine, settings=profile)


def test_harness_catches_a_planted_bug(harness: machine.Harness) -> None:
    with plant("fresh_provider_key"), pytest.raises(AssertionError, match="I3 payment"):
        run_state_machine_as_test(
            machine.LedgerlineMachine, settings=settings.get_profile("selftest")
        )
    print(f"\nplanted bug found after {harness.examples} histories")
