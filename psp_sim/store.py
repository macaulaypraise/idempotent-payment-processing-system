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
    created_at TEXT NOT NULL,
    merchant_reference TEXT
);
-- Outgoing webhooks. status: pending | delivered | dropped | failed.
-- Kept here so redelivery and inspection work across restarts.
CREATE TABLE IF NOT EXISTS webhooks (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL,
    duplicate INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    last_status INTEGER,
    created_at TEXT NOT NULL,
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_webhooks_due ON webhooks (status, next_attempt_at);
-- Faults injected into a day's settlement file through the admin API
CREATE TABLE IF NOT EXISTS settlement_faults (
    file_date TEXT NOT NULL,
    kind TEXT NOT NULL,
    psp_reference TEXT
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
    merchant_reference: str | None = None  # the merchant's refund id

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("fingerprint")
        return data


@dataclass(frozen=True)
class Webhook:
    id: str
    type: str
    body: str  # the exact JSON that is signed and sent
    status: str  # pending | delivered | dropped | failed
    duplicate: int  # 1: deliver twice (a fault)
    attempts: int
    next_attempt_at: float  # unix seconds
    last_status: int | None
    created_at: str
    delivered_at: str | None

    def public(self) -> dict[str, Any]:
        return asdict(self)


def _insert(db: sqlite3.Connection, table: str, row: dict[str, Any]) -> int:
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    cursor = db.execute(
        f"INSERT OR IGNORE INTO {table} ({columns}) VALUES ({marks})",  # noqa: S608
        tuple(row.values()),
    )
    return cursor.rowcount


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._upgrade()
        self._db.executescript(_SCHEMA)

    def _upgrade(self) -> None:
        """Bring a store created by an earlier version up to date in place."""
        columns = {
            row["name"]
            for row in self._db.execute("PRAGMA table_info(refunds)").fetchall()
        }
        if columns and "merchant_reference" not in columns:
            self._db.execute("ALTER TABLE refunds ADD COLUMN merchant_reference TEXT")

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
            created = _insert(self._db, "charges", asdict(charge)) == 1
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
        merchant_reference: str | None = None,
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
            merchant_reference=merchant_reference,
        )
        with self._lock:
            created = _insert(self._db, "refunds", asdict(refund)) == 1
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

    # --- webhooks -------------------------------------------------------------

    def insert_webhook(self, webhook: Webhook) -> None:
        with self._lock:
            _insert(self._db, "webhooks", asdict(webhook))

    def webhook(self, webhook_id: str) -> Webhook | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM webhooks WHERE id = ?", (webhook_id,)
            ).fetchone()
        return Webhook(**dict(row)) if row else None

    def webhooks(self, status: str | None = None) -> list[Webhook]:
        query = "SELECT * FROM webhooks"
        params: tuple[Any, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            params = (status,)
        with self._lock:
            rows = self._db.execute(query + " ORDER BY created_at", params).fetchall()
        return [Webhook(**dict(r)) for r in rows]

    def due_webhooks(self, now: float | None) -> list[Webhook]:
        """Pending webhooks due by `now` (all pending ones when None)."""
        query = "SELECT * FROM webhooks WHERE status = 'pending'"
        params: tuple[Any, ...] = ()
        if now is not None:
            query += " AND next_attempt_at <= ?"
            params = (now,)
        with self._lock:
            rows = self._db.execute(
                query + " ORDER BY next_attempt_at", params
            ).fetchall()
        return [Webhook(**dict(r)) for r in rows]

    def update_webhook(self, webhook_id: str, **changes: Any) -> None:
        assignments = ", ".join(f"{column} = ?" for column in changes)
        with self._lock:
            self._db.execute(
                f"UPDATE webhooks SET {assignments} WHERE id = ?",  # noqa: S608
                (*changes.values(), webhook_id),
            )

    # --- settlement faults ------------------------------------------------------

    def add_settlement_fault(
        self, file_date: str, kind: str, psp_reference: str | None
    ) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO settlement_faults VALUES (?, ?, ?)",
                (file_date, kind, psp_reference),
            )

    def settlement_faults(self, file_date: str) -> list[tuple[str, str | None]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT kind, psp_reference FROM settlement_faults WHERE file_date = ?",
                (file_date,),
            ).fetchall()
        return [(r["kind"], r["psp_reference"]) for r in rows]

    def clear_settlement_faults(self, file_date: str) -> None:
        with self._lock:
            self._db.execute(
                "DELETE FROM settlement_faults WHERE file_date = ?", (file_date,)
            )

    def reset(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM charges")
            self._db.execute("DELETE FROM refunds")
            self._db.execute("DELETE FROM webhooks")
            self._db.execute("DELETE FROM settlement_faults")
