"""SQLite-backed ground truth: what the simulator actually charged and
refunded. It survives restarts, so it stays the oracle across crash tests."""

import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS charges (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    fingerprint TEXT NOT NULL,
    merchant_reference TEXT NOT NULL,
    amount INTEGER NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL,
    decline_reason TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_charges_reference ON charges (merchant_reference);
CREATE TABLE IF NOT EXISTS refunds (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    fingerprint TEXT NOT NULL,
    charge_id TEXT NOT NULL,
    amount INTEGER NOT NULL,
    status TEXT NOT NULL,
    failure_reason TEXT,
    created_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Charge:
    id: str
    idempotency_key: str
    fingerprint: str
    merchant_reference: str
    amount: int
    currency: str
    status: str  # succeeded | declined
    decline_reason: str | None
    created_at: str

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("fingerprint")
        return data


@dataclass(frozen=True)
class Refund:
    id: str
    idempotency_key: str
    fingerprint: str
    charge_id: str
    amount: int
    status: str  # succeeded | failed
    failure_reason: str | None
    created_at: str

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("fingerprint")
        return data


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)

    def charge_by_key(self, key: str) -> Charge | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM charges WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return Charge(**dict(row)) if row else None

    def charge_by_id(self, charge_id: str) -> Charge | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM charges WHERE id = ?", (charge_id,)
            ).fetchone()
        return Charge(**dict(row)) if row else None

    def charges_by_reference(self, reference: str) -> list[Charge]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM charges WHERE merchant_reference = ? "
                "ORDER BY created_at",
                (reference,),
            ).fetchall()
        return [Charge(**dict(r)) for r in rows]

    def insert_charge(
        self,
        *,
        key: str,
        fingerprint: str,
        merchant_reference: str,
        amount: int,
        currency: str,
        status: str,
        decline_reason: str | None,
    ) -> tuple[Charge, bool]:
        """Insert unless the key exists; return (charge, created)."""
        charge = Charge(
            id=f"ch_{uuid.uuid4().hex[:24]}",
            idempotency_key=key,
            fingerprint=fingerprint,
            merchant_reference=merchant_reference,
            amount=amount,
            currency=currency,
            status=status,
            decline_reason=decline_reason,
            created_at=_now(),
        )
        with self._lock:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO charges VALUES (?,?,?,?,?,?,?,?,?)",
                tuple(asdict(charge).values()),
            )
            created = cursor.rowcount == 1
        if created:
            return charge, True
        existing = self.charge_by_key(key)
        assert existing is not None
        return existing, False

    def refund_by_key(self, key: str) -> Refund | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM refunds WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return Refund(**dict(row)) if row else None

    def succeeded_refund_for(self, charge_id: str) -> Refund | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM refunds WHERE charge_id = ? AND status = 'succeeded'",
                (charge_id,),
            ).fetchone()
        return Refund(**dict(row)) if row else None

    def insert_refund(
        self,
        *,
        key: str,
        fingerprint: str,
        charge_id: str,
        amount: int,
        status: str,
        failure_reason: str | None,
    ) -> tuple[Refund, bool]:
        refund = Refund(
            id=f"re_{uuid.uuid4().hex[:24]}",
            idempotency_key=key,
            fingerprint=fingerprint,
            charge_id=charge_id,
            amount=amount,
            status=status,
            failure_reason=failure_reason,
            created_at=_now(),
        )
        with self._lock:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO refunds VALUES (?,?,?,?,?,?,?,?)",
                tuple(asdict(refund).values()),
            )
            created = cursor.rowcount == 1
        if created:
            return refund, True
        existing = self.refund_by_key(key)
        assert existing is not None
        return existing, False

    def ground_truth(self) -> dict[str, list[dict[str, Any]]]:
        with self._lock:
            charges = self._db.execute(
                "SELECT * FROM charges ORDER BY created_at"
            ).fetchall()
            refunds = self._db.execute(
                "SELECT * FROM refunds ORDER BY created_at"
            ).fetchall()
        return {
            "charges": [Charge(**dict(r)).public() for r in charges],
            "refunds": [Refund(**dict(r)).public() for r in refunds],
        }

    def reset(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM charges")
            self._db.execute("DELETE FROM refunds")
