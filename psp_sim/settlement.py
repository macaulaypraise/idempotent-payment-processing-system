"""Daily settlement files (DESIGN 6.6, project guide D).

The file for a UTC date lists every charge and refund that succeeded that
day: one line each with gross, fee and net. Refund lines have no fee and net
equals gross; the type says the money goes back.

A file is a deterministic function of the store, the seed, the fault
profile and any explicitly injected faults: line ids are the provider
references, and the random fault decision for a line is drawn from an RNG
seeded by (seed, reference). So fetching a date twice gives the same lines,
and a file grows only by appending the day's later transactions.

Faults (a real provider's files are wrong in exactly these ways):
  amount_mismatch  the line's gross is 100 higher (fee and net follow)
  missing          the line is left out
  duplicate        the line appears twice, the copy with line id "<ref>#dup"
  extra            a line for a charge we never made
"""

import csv
import hashlib
import io
import random
from dataclasses import asdict, dataclass, replace
from datetime import date
from typing import Any

from psp_sim.faults import FaultProfile

COLUMNS = (
    "line_id",
    "type",
    "psp_reference",
    "merchant_reference",
    "gross",
    "fee",
    "net",
    "currency",
)
FAULT_KINDS = ("amount_mismatch", "missing", "duplicate", "extra")
AMOUNT_SKEW = 100


@dataclass(frozen=True)
class Line:
    line_id: str
    type: str  # charge | refund
    psp_reference: str
    merchant_reference: str
    gross: int
    fee: int
    net: int
    currency: str


@dataclass(frozen=True)
class Fault:
    kind: str
    psp_reference: str | None = None  # None for "extra"


def fee_for(gross: int) -> int:
    """2.9% + 30 minor units, rounded half up, always below gross."""
    return max(0, min(gross - 1, (gross * 29 + 500) // 1000 + 30))


def charge_line(charge: dict[str, Any]) -> Line:
    fee = fee_for(charge["amount"])
    return Line(
        line_id=charge["id"],
        type="charge",
        psp_reference=charge["id"],
        merchant_reference=charge["merchant_reference"],
        gross=charge["amount"],
        fee=fee,
        net=charge["amount"] - fee,
        currency=charge["currency"],
    )


def refund_line(refund: dict[str, Any], currency: str) -> Line:
    return Line(
        line_id=refund["id"],
        type="refund",
        psp_reference=refund["id"],
        merchant_reference=refund.get("merchant_reference") or "",
        gross=refund["amount"],
        fee=0,
        net=refund["amount"],
        currency=currency,
    )


def _skewed(line: Line) -> Line:
    gross = line.gross + AMOUNT_SKEW
    fee = fee_for(gross) if line.type == "charge" else 0
    return replace(line, gross=gross, fee=fee, net=gross - fee)


def _extra(day: date, n: int, seed: int) -> Line:
    digest = hashlib.sha256(f"{seed}:{day}:{n}".encode()).hexdigest()
    gross = 1_000 + int(digest[:4], 16) % 9_000
    fee = fee_for(gross)
    return Line(
        line_id=f"extra-{day}-{n}",
        type="charge",
        psp_reference=f"ch_unknown_{digest[:16]}",
        merchant_reference=f"unknown-{digest[16:28]}",
        gross=gross,
        fee=fee,
        net=gross - fee,
        currency="USD",
    )


def _random_faults(
    lines: list[Line], day: date, profile: FaultProfile, seed: int
) -> list[Fault]:
    faults: list[Fault] = []
    for line in lines:
        rng = random.Random(f"{seed}:{line.psp_reference}")
        if rng.random() < profile.settlement_missing_rate:
            faults.append(Fault("missing", line.psp_reference))
        elif rng.random() < profile.settlement_amount_mismatch_rate:
            faults.append(Fault("amount_mismatch", line.psp_reference))
        elif rng.random() < profile.settlement_duplicate_rate:
            faults.append(Fault("duplicate", line.psp_reference))
    if random.Random(f"{seed}:{day}:extra").random() < profile.settlement_extra_rate:
        faults.append(Fault("extra"))
    return faults


def build_file(
    day: date,
    charges: list[dict[str, Any]],
    refunds: list[dict[str, Any]],
    *,
    profile: FaultProfile,
    seed: int,
    injected: list[Fault],
) -> list[Line]:
    """Lines for `day`: successful charges and refunds created that day."""
    by_id = {c["id"]: c for c in charges}
    clean = [
        charge_line(c)
        for c in charges
        if c["status"] == "succeeded" and c["created_at"][:10] == day.isoformat()
    ] + [
        refund_line(
            r, by_id[r["charge_id"]]["currency"] if r["charge_id"] in by_id else "USD"
        )
        for r in refunds
        if r["status"] == "succeeded" and r["created_at"][:10] == day.isoformat()
    ]
    faults = _random_faults(clean, day, profile, seed) + injected
    kinds: dict[str, set[str]] = {}
    for fault in faults:
        if fault.psp_reference is not None:
            kinds.setdefault(fault.psp_reference, set()).add(fault.kind)

    lines: list[Line] = []
    for line in clean:
        applied = kinds.get(line.psp_reference, set())
        if "missing" in applied:
            continue
        if "amount_mismatch" in applied:
            line = _skewed(line)
        lines.append(line)
        if "duplicate" in applied:
            lines.append(replace(line, line_id=f"{line.line_id}#dup"))
    extras = sum(1 for f in faults if f.kind == "extra")
    lines.extend(_extra(day, n, seed) for n in range(extras))
    return lines


def render_csv(lines: list[Line]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    for line in lines:
        writer.writerow(asdict(line))
    return out.getvalue()
