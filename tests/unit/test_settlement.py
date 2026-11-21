"""Settlement files: the simulator's generator and our parser."""

import csv
import io
from collections.abc import AsyncIterator
from datetime import date
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.services.reconciliation import SettlementFileError, parse_file
from app.workers.recon_job import parse_args
from psp_sim.faults import FaultProfile
from psp_sim.main import create_app
from psp_sim.settlement import Fault, build_file, fee_for, render_csv

DAY = date(2026, 11, 21)


def charge(
    ref: str, amount: int = 1_000, status: str = "succeeded", day: date = DAY
) -> dict[str, Any]:
    return {
        "id": ref,
        "merchant_reference": f"pay-{ref}",
        "amount": amount,
        "currency": "USD",
        "status": status,
        "created_at": f"{day.isoformat()}T10:00:00+00:00",
    }


def refund(ref: str, charge_id: str, amount: int = 1_000) -> dict[str, Any]:
    return {
        "id": ref,
        "charge_id": charge_id,
        "merchant_reference": f"rf-{ref}",
        "amount": amount,
        "status": "succeeded",
        "created_at": f"{DAY.isoformat()}T11:00:00+00:00",
    }


def build(
    charges: list[dict[str, Any]], refunds: list[dict[str, Any]] = [], **kw: Any
) -> list[Any]:  # noqa: B006
    return build_file(
        DAY,
        charges,
        refunds,
        profile=kw.pop("profile", FaultProfile()),
        seed=kw.pop("seed", 0),
        injected=kw.pop("injected", []),
    )


@pytest.mark.parametrize(
    ("gross", "fee"), [(1_000, 59), (2_500, 103), (100, 33), (31, 30), (20, 19), (1, 0)]
)
def test_fee_is_2_9_percent_plus_30_and_always_below_gross(
    gross: int, fee: int
) -> None:
    assert fee_for(gross) == fee
    assert 0 <= fee_for(gross) < gross or gross == 1


def test_only_the_days_successful_charges_and_refunds_are_listed() -> None:
    lines = build(
        [
            charge("ch_1"),
            charge("ch_2", status="declined"),
            charge("ch_3", day=date(2026, 11, 20)),
        ],
        [refund("re_1", "ch_1")],
    )
    assert [(ln.line_id, ln.type) for ln in lines] == [
        ("ch_1", "charge"),
        ("re_1", "refund"),
    ]
    c, r = lines
    assert (c.gross, c.fee, c.net, c.merchant_reference) == (1_000, 59, 941, "pay-ch_1")
    assert (r.gross, r.fee, r.net, r.currency) == (1_000, 0, 1_000, "USD")


def test_injected_faults() -> None:
    lines = build(
        [charge("ch_a"), charge("ch_b"), charge("ch_c"), charge("ch_d")],
        injected=[
            Fault("amount_mismatch", "ch_a"),
            Fault("missing", "ch_b"),
            Fault("duplicate", "ch_c"),
            Fault("extra"),
        ],
    )
    ids = [ln.line_id for ln in lines]
    assert ids[:4] == ["ch_a", "ch_c", "ch_c#dup", "ch_d"]
    assert ids[4].startswith("extra-2026-11-21-")
    assert (lines[0].gross, lines[0].net) == (1_100, 1_100 - fee_for(1_100))
    assert lines[1].psp_reference == lines[2].psp_reference == "ch_c"
    assert lines[4].psp_reference.startswith("ch_unknown_")


def test_random_faults_are_stable_per_reference() -> None:
    """The same reference gets the same decision however many other lines
    the day has, so a growing file never changes its earlier lines."""
    profile = FaultProfile(
        settlement_missing_rate=0.3,
        settlement_amount_mismatch_rate=0.3,
        settlement_duplicate_rate=0.3,
    )
    small = build([charge(f"ch_{i}") for i in range(10)], profile=profile, seed=4)
    large = build([charge(f"ch_{i}") for i in range(40)], profile=profile, seed=4)
    assert render_csv(small) == render_csv(
        build([charge(f"ch_{i}") for i in range(10)], profile=profile, seed=4)
    )
    small_ids = {ln.line_id: ln for ln in small}
    assert all(small_ids[ln.line_id] == ln for ln in large if ln.line_id in small_ids)
    kinds = {ln.line_id for ln in large}
    assert any(k.endswith("#dup") for k in kinds)
    assert len([k for k in kinds if not k.endswith("#dup")]) < 40  # some missing
    assert build([charge("ch_x")], profile=FaultProfile(settlement_extra_rate=1.0))[
        -1
    ].line_id.startswith("extra-")


def test_csv_round_trips_through_our_parser() -> None:
    lines = build([charge("ch_1", 2_500)], [refund("re_1", "ch_1", 2_500)])
    rows = parse_file(render_csv(lines))
    assert rows == [
        {
            "line_id": "ch_1",
            "type": "charge",
            "psp_reference": "ch_1",
            "merchant_reference": "pay-ch_1",
            "gross": 2_500,
            "fee": 103,
            "net": 2_397,
            "currency": "USD",
        },
        {
            "line_id": "re_1",
            "type": "refund",
            "psp_reference": "re_1",
            "merchant_reference": "rf-re_1",
            "gross": 2_500,
            "fee": 0,
            "net": 2_500,
            "currency": "USD",
        },
    ]


HEADER = "line_id,type,psp_reference,merchant_reference,gross,fee,net,currency\n"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("a,b\n", "missing columns"),
        (HEADER + "1,payout,p,m,1,0,1,USD\n", "unknown type"),
        (HEADER + ",charge,p,m,1,0,1,USD\n", "required"),
        (HEADER + "1,charge,p,m,one,0,1,USD\n", "line 2"),
    ],
)
def test_parser_rejects_malformed_files(body: str, message: str) -> None:
    with pytest.raises(SettlementFileError, match=message):
        parse_file(body)


def test_empty_merchant_reference_becomes_none_and_currency_upper() -> None:
    [row] = parse_file(HEADER + "1,refund,re,,5,0,5,usd\n")
    assert row["merchant_reference"] is None and row["currency"] == "USD"


def test_recon_job_takes_several_dates_in_order() -> None:
    args = parse_args(["--date", "2026-11-20", "--date", "2026-11-21"])
    assert args.dates == [date(2026, 11, 20), date(2026, 11, 21)]
    with pytest.raises(SystemExit):
        parse_args(["--date", "21/11/2026"])


# --- the simulator's endpoints ----------------------------------------------------------


@pytest_asyncio.fixture
async def sim() -> AsyncIterator[AsyncClient]:
    app = create_app(db_path=":memory:")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sim"
    ) as c:
        yield c


async def test_settlement_endpoint_serves_the_days_csv(sim: AsyncClient) -> None:
    created = await sim.post(
        "/charges",
        json={"amount": 1_000, "currency": "USD", "merchant_reference": "pay_1"},
        headers={"Idempotency-Key": "k"},
    )
    day = created.json()["created_at"][:10]
    resp = await sim.get(f"/settlements/{day}")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith(
        "text/csv"
    )
    [row] = list(csv.DictReader(io.StringIO(resp.text)))
    assert row["psp_reference"] == created.json()["id"] and row["net"] == "941"
    assert (await sim.get("/settlements/2001-01-01")).text == HEADER
    assert (await sim.get("/settlements/not-a-date")).status_code == 422


async def test_faults_can_be_injected_and_cleared(sim: AsyncClient) -> None:
    created = await sim.post(
        "/charges",
        json={"amount": 1_000, "currency": "USD", "merchant_reference": "pay_1"},
        headers={"Idempotency-Key": "k"},
    )
    ref, day = created.json()["id"], created.json()["created_at"][:10]
    resp = await sim.post(
        f"/admin/settlements/{day}/faults",
        json={"faults": [{"kind": "missing", "psp_reference": ref}, {"kind": "extra"}]},
    )
    assert resp.json() == {"file_date": day, "faults": 2}
    rows = list(
        csv.DictReader(io.StringIO((await sim.get(f"/settlements/{day}")).text))
    )
    assert [r["line_id"].startswith("extra-") for r in rows] == [True]

    bad = await sim.post(
        f"/admin/settlements/{day}/faults", json={"faults": [{"kind": "missing"}]}
    )
    assert bad.status_code == 422
    await sim.delete(f"/admin/settlements/{day}/faults")
    rows = list(
        csv.DictReader(io.StringIO((await sim.get(f"/settlements/{day}")).text))
    )
    assert [r["psp_reference"] for r in rows] == [ref]

    await sim.post(
        f"/admin/settlements/{day}/faults", json={"faults": [{"kind": "extra"}]}
    )
    await sim.post("/admin/reset")
    assert (await sim.get(f"/settlements/{day}")).text == HEADER
