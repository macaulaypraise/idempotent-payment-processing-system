"""The harness's own pieces, without a database: history files, replay
identity (I9), and the model's expectations once the system is quiet."""

import uuid
from typing import Any

import pytest

from app.core.planted_bugs import plant, planted
from verification.checker import replays
from verification.history import History, outcome_for
from verification.model import Model

W1, W2 = uuid.UUID(int=1), uuid.UUID(int=2)
P1, P2, P3 = uuid.UUID(int=11), uuid.UUID(int=12), uuid.UUID(int=13)
R1 = uuid.UUID(int=21)


def history(*ops: tuple[Any, ...]) -> History:
    h = History()
    for op, key, request, status, body in ops:
        h.record("client", op, key=key, request=request, status=status, body=body)
    return h


def pay(
    key: str, payment_id: uuid.UUID, amount: int, wallet: uuid.UUID = W1
) -> tuple[Any, ...]:
    body = {"amount": amount, "currency": "USD", "wallet_id": str(wallet)}
    return (
        "create_payment",
        key,
        body,
        202,
        {"payment_id": str(payment_id), "status": "PENDING"},
    )


@pytest.mark.parametrize(
    ("status", "outcome"),
    [(200, "ok"), (202, "ok"), (422, "fail"), (503, "unknown"), (None, "unknown")],
)
def test_outcomes(status: int | None, outcome: str) -> None:
    assert outcome_for(status) == outcome


def test_history_round_trips_through_jsonl(tmp_path: Any) -> None:
    h = history(pay("k1", P1, 500))
    h.record("fault", "set_faults", request={"decline_rate": 0.5}, status=200)
    loaded = History.load(h.dump(tmp_path / "h.jsonl"))
    assert loaded.operations == h.operations
    assert [o.op for o in loaded.client()] == ["create_payment"]


def test_identical_replays_pass() -> None:
    assert replays(history(pay("k1", P1, 500), pay("k1", P1, 500))) == []


def test_a_replay_with_a_different_answer_is_an_i9_violation() -> None:
    [violation] = replays(history(pay("k1", P1, 500), pay("k1", P2, 500)))
    assert violation.startswith("I9 create_payment key 'k1': replay #1")


def test_a_different_body_must_be_refused_with_422() -> None:
    other = ("create_payment", "k1", {"amount": 501}, 422, {"detail": "mismatch"})
    assert replays(history(pay("k1", P1, 500), other)) == []
    accepted = ("create_payment", "k1", {"amount": 501}, 202, {"payment_id": str(P2)})
    [violation] = replays(history(pay("k1", P1, 500), accepted))
    assert "expected 422" in violation


def test_unknown_outcomes_are_not_compared() -> None:
    lost = ("create_payment", "k1", {"amount": 500}, None, "ReadTimeout")
    assert replays(history(lost, pay("k1", P1, 500))) == []


def test_model_counts_each_acknowledged_write_once() -> None:
    h = history(
        ("create_wallet", "w1", {"currency": "USD"}, 201, {"wallet_id": str(W1)}),
        pay("k1", P1, 500),
        pay("k1", P1, 500),  # a replay
        ("create_payment", "k2", {"amount": 9}, 422, {"detail": "x"}),  # rejected
        ("create_refund", "r1", {"payment_id": str(P1)}, 201, {"refund_id": str(R1)}),
        (
            "create_transfer",
            "t1",
            {"from_wallet_id": str(W1), "to_wallet_id": str(W2), "amount": 7},
            201,
            {"transfer_id": "t"},
        ),
    )
    model = Model.from_history(h)
    assert model.wallets == [W1]
    assert list(model.payments) == [P1] and model.payments[P1].amount == 500
    assert list(model.refunds) == [R1] and len(model.transfers) == 1


def truth(
    *charges: tuple[uuid.UUID, str], refunds: tuple[tuple[uuid.UUID, str], ...] = ()
) -> dict[str, Any]:
    return {
        "charges": [{"merchant_reference": str(p), "status": s} for p, s in charges],
        "refunds": [{"merchant_reference": str(r), "status": s} for r, s in refunds],
    }


def quiet_model() -> Model:
    h = history(
        ("create_wallet", "w1", {"currency": "USD"}, 201, {"wallet_id": str(W1)}),
        ("create_wallet", "w2", {"currency": "USD"}, 201, {"wallet_id": str(W2)}),
        pay("k1", P1, 1_000),
        pay("k2", P2, 700),
        pay("k3", P3, 300),
        ("create_refund", "r1", {"payment_id": str(P2)}, 201, {"refund_id": str(R1)}),
        (
            "create_transfer",
            "t1",
            {"from_wallet_id": str(W1), "to_wallet_id": str(W2), "amount": 250},
            201,
            {},
        ),
    )
    return Model.from_history(h)


def test_a_consistent_quiet_system_has_no_violations() -> None:
    violations = quiet_model().quiescent_violations(
        payments={
            P1: {"status": "SETTLED", "amount": 1_000},
            P2: {"status": "REFUNDED", "amount": 700},
            P3: {"status": "FAILED", "amount": 300},
        },
        refunds={R1: {"status": "SUCCEEDED", "amount": 700}},
        balances={W1: (0, 1_000 - 250), W2: (0, 250)},
        ground_truth=truth(
            (P1, "succeeded"),
            (P2, "succeeded"),
            (P3, "declined"),
            refunds=((R1, "succeeded"),),
        ),
    )
    assert violations == []


def test_the_model_catches_wrong_states_and_balances() -> None:
    violations = quiet_model().quiescent_violations(
        payments={
            P1: {"status": "FAILED", "amount": 1_000},  # but the provider charged it
            P3: {"status": "SETTLED", "amount": 300},  # but the provider declined it
        },
        refunds={R1: {"status": "PROCESSING", "amount": 700}},  # but it succeeded
        balances={W1: (5, 1_000 - 250), W2: (0, 999)},
        ground_truth=truth(
            (P1, "succeeded"),
            (P2, "succeeded"),
            (P3, "declined"),
            refunds=((R1, "succeeded"),),
        ),
    )
    text = "\n".join(violations)
    assert (
        "payment 00000000-0000-0000-0000-00000000000b is FAILED but the provider says succeeded"
        in text
    )
    assert (
        "I10 acknowledged payment 00000000-0000-0000-0000-00000000000c is missing"
        in text
    )
    assert "is SETTLED but the provider says declined" in text
    assert "refund 00000000-0000-0000-0000-000000000015 is PROCESSING" in text
    assert "pending 5 != 0" in text and "available 999 != expected 250" in text


def test_a_payment_never_charged_must_be_left_processing() -> None:
    model = Model.from_history(history(pay("k1", P1, 100)))
    ok = model.quiescent_violations(
        {P1: {"status": "PROCESSING", "amount": 100}}, {}, {}, truth()
    )
    bad = model.quiescent_violations(
        {P1: {"status": "PENDING", "amount": 100}}, {}, {}, truth()
    )
    assert ok == [] and "no charge" in bad[0]


def test_planted_bugs_are_off_by_default_named_and_never_in_production(
    monkeypatch: Any,
) -> None:
    from app.config import get_settings
    from app.core import planted_bugs

    assert not planted("fresh_provider_key")
    with plant("fresh_provider_key"):
        assert planted("fresh_provider_key")
        production = get_settings().model_copy(update={"APP_ENV": "production"})
        monkeypatch.setattr(planted_bugs, "get_settings", lambda: production)
        assert not planted("fresh_provider_key")
    with pytest.raises(ValueError, match="unknown planted bug"):
        with plant("nope"):
            pass
