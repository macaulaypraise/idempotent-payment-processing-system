"""Daily reconciliation against the provider's settlement file (DESIGN 6.6).

A run for one file date:
  1. takes a session advisory lock for (provider, date), so two runs of the
     same date cannot interleave and classify duplicates differently; a run
     left `running` by a crash is marked `abandoned`;
  2. downloads the file and loads it idempotently into settlement_lines;
  3. reconciles each line in its own transaction, in line-id order: lock the
     payment (or refund), classify, and for a match post the settlement and
     release transfers and record which line settled the row;
  4. reports every capture or refund dated (file date - window) that no
     line in any loaded file mentions, as missing_at_psp;
  5. writes the counts.

Matching uses the provider reference, then our merchant reference. Never
the amount. Only matched (and healed) lines post anything; every other
classification is reported for a human and nothing is corrected
automatically. Every posting is unique per payment or refund, and the
settling line is recorded once, so re-running a date posts nothing.

The full rules, with examples, are in docs/reconciliation.md.
"""

import csv
import io
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta

import structlog
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.psp import PspClient
from app.config import Settings
from app.core.clock import Clock
from app.core.failpoints import failpoint
from app.core.metrics import (
    recon_items_total,
    recon_last_success_timestamp,
    recon_unexplained_items,
)
from app.models.payment import Payment, PaymentStatus
from app.models.reconciliation import (
    EXPLAINED,
    Classification,
    ReconItem,
    ReconRun,
    RunStatus,
    SettlementLine,
)
from app.models.refund import Refund, RefundStatus
from app.services import posting_rules
from app.services.ledger import Reference, post_transfer
from app.services.psp_results import (
    ConflictingOutcomeError,
    OutcomeKind,
    PspOutcome,
    RefundOutcome,
    RefundOutcomeKind,
    apply_psp_result,
    apply_refund_result,
    lock_payment,
    lock_refund,
    payment_accounts,
)

logger = structlog.get_logger()

SOURCE = "reconciliation"
_LOCK_CLASS = 0x5EC0  # pg_try_advisory_lock(int, int): (class, date ordinal)
ACTIVE_REFUNDS = (RefundStatus.PENDING, RefundStatus.PROCESSING)
CSV_COLUMNS = {
    "line_id",
    "type",
    "psp_reference",
    "merchant_reference",
    "gross",
    "fee",
    "net",
    "currency",
}


class ReconInProgressError(Exception):
    """Another run holds this date's lock."""


class SettlementFileError(Exception):
    """The file could not be parsed; nothing from it was loaded."""


@dataclass(frozen=True)
class Verdict:
    classification: Classification
    tenant_id: uuid.UUID | None = None
    payment_id: uuid.UUID | None = None
    refund_id: uuid.UUID | None = None
    expected: int | None = None
    actual: int | None = None
    posted: int = 0
    detail: str | None = None


def parse_file(content: str) -> list[dict[str, object]]:
    """Parse a settlement CSV into row dicts; raise on any malformed row."""
    reader = csv.DictReader(io.StringIO(content))
    missing = CSV_COLUMNS - set(reader.fieldnames or [])
    if missing:
        raise SettlementFileError(f"missing columns: {sorted(missing)}")
    rows: list[dict[str, object]] = []
    for number, row in enumerate(reader, start=2):
        try:
            if row["type"] not in ("charge", "refund"):
                raise ValueError(f"unknown type {row['type']!r}")
            if not row["line_id"] or not row["psp_reference"]:
                raise ValueError("line_id and psp_reference are required")
            rows.append(
                {
                    "line_id": row["line_id"],
                    "type": row["type"],
                    "psp_reference": row["psp_reference"],
                    "merchant_reference": row["merchant_reference"] or None,
                    "gross": int(row["gross"]),
                    "fee": int(row["fee"]),
                    "net": int(row["net"]),
                    "currency": row["currency"].upper(),
                }
            )
        except (ValueError, KeyError, AttributeError) as exc:
            raise SettlementFileError(f"line {number}: {exc}") from exc
    return rows


def _uuid(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


def _amount_problem(line: SettlementLine, amount: int, currency: str) -> str | None:
    if line.currency != currency:
        return f"currency {line.currency} != {currency}"
    if line.gross != amount:
        return f"gross {line.gross} != {amount}"
    if line.net != line.gross - line.fee:
        return f"net {line.net} != gross {line.gross} - fee {line.fee}"
    if line.type == "charge" and not 0 <= line.fee < line.gross:
        return f"fee {line.fee} outside [0, gross)"
    if line.type == "refund" and line.fee != 0:
        return f"refund line carries a fee ({line.fee}); no posting rule for it"
    return None


@dataclass
class Reconciler:
    session_factory: async_sessionmaker[AsyncSession]
    psp: PspClient
    clock: Clock
    settings: Settings

    @property
    def provider(self) -> str:
        return self.settings.RECON_PROVIDER

    # --- the run ---------------------------------------------------------------

    async def run(self, file_date: date) -> ReconRun:
        """Reconcile one file date end to end; returns the finished run."""
        async with self.session_factory() as lock_session:
            # Autocommit: holding the lock must not hold a transaction open
            conn = await lock_session.connection(
                execution_options={"isolation_level": "AUTOCOMMIT"}
            )
            key = {"c": _LOCK_CLASS, "d": file_date.toordinal()}
            locked = (
                await conn.execute(text("SELECT pg_try_advisory_lock(:c, :d)"), key)
            ).scalar_one()
            if not locked:
                raise ReconInProgressError(f"a run for {file_date} is in progress")
            try:
                return await self._run_locked(file_date)
            finally:
                await conn.execute(text("SELECT pg_advisory_unlock(:c, :d)"), key)

    async def _run_locked(self, file_date: date) -> ReconRun:
        run_id = await self._start(file_date)
        try:
            content = await self.psp.settlement_file(file_date)
            lines = await self.load_file(file_date, content)
            verdicts: list[Verdict] = []
            for line in lines:
                verdicts.append(await self.reconcile_line(run_id, line))
                # Each line is committed: a crash here resumes on re-run
                failpoint("recon.after_line")
            verdicts.extend(await self.find_missing_at_psp(run_id, file_date))
        except Exception as exc:
            await self._fail(run_id, exc)
            raise
        return await self._finish(run_id, len(lines), verdicts)

    async def _start(self, file_date: date) -> uuid.UUID:
        async with self.session_factory() as db:
            # We hold the date's lock, so any run still marked running is dead
            await db.execute(
                update(ReconRun)
                .where(
                    ReconRun.provider == self.provider,
                    ReconRun.file_date == file_date,
                    ReconRun.status == RunStatus.RUNNING,
                )
                .values(status=RunStatus.ABANDONED, finished_at=self.clock.now())
            )
            run = ReconRun(
                id=uuid.uuid4(),
                provider=self.provider,
                file_date=file_date,
                status=RunStatus.RUNNING,
                started_at=self.clock.now(),
            )
            db.add(run)
            await db.commit()
            run_id: uuid.UUID = run.id
            return run_id

    async def _fail(self, run_id: uuid.UUID, exc: Exception) -> None:
        async with self.session_factory() as db:
            await db.execute(
                update(ReconRun)
                .where(ReconRun.id == run_id)
                .values(
                    status=RunStatus.FAILED,
                    error=f"{type(exc).__name__}: {exc}"[:2000],
                    finished_at=self.clock.now(),
                )
            )
            await db.commit()
        logger.error("recon_run_failed", run_id=str(run_id), error=str(exc))

    async def _finish(
        self, run_id: uuid.UUID, lines: int, verdicts: list[Verdict]
    ) -> ReconRun:
        counts = Counter(v.classification.value for v in verdicts)
        unexplained = sum(1 for v in verdicts if v.classification not in EXPLAINED)
        async with self.session_factory() as db:
            run = await db.get(ReconRun, run_id)
            assert run is not None
            run.status = RunStatus.COMPLETED
            run.lines = lines
            run.counts = dict(sorted(counts.items()))
            run.unexplained = unexplained
            run.posted = sum(v.posted for v in verdicts)
            run.finished_at = self.clock.now()
            await db.commit()
        for classification, n in counts.items():
            recon_items_total.labels(classification=classification).inc(n)
        recon_unexplained_items.set(unexplained)
        recon_last_success_timestamp.set(self.clock.now().timestamp())
        logger.info(
            "recon_run_completed",
            run_id=str(run_id),
            file_date=str(run.file_date),
            lines=lines,
            counts=run.counts,
            unexplained=unexplained,
            posted=run.posted,
        )
        return run

    # --- load ------------------------------------------------------------------

    async def load_file(self, file_date: date, content: str) -> list[SettlementLine]:
        """Insert the file's lines (a line already loaded is kept as first
        loaded) and return all of the date's lines in line-id order."""
        rows = parse_file(content)
        async with self.session_factory() as db:
            if rows:
                await db.execute(
                    insert(SettlementLine)
                    .values(
                        [
                            {
                                "id": uuid.uuid4(),
                                "provider": self.provider,
                                "file_date": file_date,
                                **row,
                            }
                            for row in rows
                        ]
                    )
                    .on_conflict_do_nothing(constraint="uq_settlement_lines_line")
                )
            lines = (
                (
                    await db.execute(
                        select(SettlementLine)
                        .where(
                            SettlementLine.provider == self.provider,
                            SettlementLine.file_date == file_date,
                        )
                        .order_by(SettlementLine.line_id)
                    )
                )
                .scalars()
                .all()
            )
            await db.commit()
        return list(lines)

    # --- one line, one transaction ---------------------------------------------

    async def reconcile_line(self, run_id: uuid.UUID, line: SettlementLine) -> Verdict:
        async with self.session_factory() as db:
            if line.type == "charge":
                verdict = await self._charge_line(db, line)
            else:
                verdict = await self._refund_line(db, line)
            await db.execute(
                insert(ReconItem)
                .values(
                    id=uuid.uuid4(),
                    run_id=run_id,
                    settlement_line_id=line.id,
                    classification=verdict.classification,
                    tenant_id=verdict.tenant_id,
                    payment_id=verdict.payment_id,
                    refund_id=verdict.refund_id,
                    expected=verdict.expected,
                    actual=verdict.actual,
                    posted=verdict.posted,
                    detail=verdict.detail,
                    created_at=self.clock.now(),
                )
                .on_conflict_do_nothing(
                    index_elements=["run_id", "settlement_line_id"],
                    index_where=text("settlement_line_id IS NOT NULL"),
                )
            )
            await db.commit()
        return verdict

    async def _find_payment(
        self, db: AsyncSession, line: SettlementLine
    ) -> Payment | None:
        found = (
            await db.execute(
                select(Payment).where(Payment.psp_reference == line.psp_reference)
            )
        ).scalar_one_or_none()
        if found is None and (payment_id := _uuid(line.merchant_reference)):
            found = await db.get(Payment, payment_id)
        if found is None:
            return None
        return await lock_payment(db, tenant_id=found.tenant_id, payment_id=found.id)

    async def _charge_line(self, db: AsyncSession, line: SettlementLine) -> Verdict:
        payment = await self._find_payment(db, line)
        if payment is None:
            return Verdict(
                Classification.MISSING_IN_LEDGER,
                actual=line.gross,
                detail=f"no payment for {line.psp_reference} / "
                f"{line.merchant_reference}",
            )
        base = {"tenant_id": payment.tenant_id, "payment_id": payment.id}
        if payment.wallet_id is None:
            return Verdict(
                Classification.STATUS_CONFLICT, **base, detail="pre-ledger payment"
            )
        if payment.psp_reference not in (None, line.psp_reference):
            return Verdict(
                Classification.STATUS_CONFLICT,
                **base,
                detail=f"recorded charge {payment.psp_reference}, "
                f"line {line.psp_reference}",
            )
        problem = _amount_problem(line, payment.amount, payment.currency)
        if problem:
            return Verdict(
                Classification.AMOUNT_MISMATCH,
                **base,
                expected=payment.amount,
                actual=line.gross,
                detail=problem,
            )
        if payment.settlement_line_id == line.id:
            return Verdict(
                Classification.MATCHED, **base, detail="already settled by this line"
            )
        if payment.settlement_line_id is not None:
            return Verdict(
                Classification.DUPLICATE_LINE,
                **base,
                actual=line.gross,
                detail=f"already settled by line {payment.settlement_line_id}",
            )

        healed = False
        if payment.status is PaymentStatus.PROCESSING:
            # The provider charged; our charger never learned it (response
            # lost, webhook dropped). Same entry point as every other source.
            await apply_psp_result(
                db,
                tenant_id=payment.tenant_id,
                payment_id=payment.id,
                outcome=PspOutcome(OutcomeKind.CAPTURED, line.psp_reference),
                source=SOURCE,
            )
            healed = True
        if payment.status in (PaymentStatus.PENDING, PaymentStatus.FAILED):
            return Verdict(
                Classification.STATUS_CONFLICT,
                **base,
                expected=0,
                actual=line.gross,
                detail=f"provider settled a charge for a {payment.status.name} payment",
            )

        posted = await self._settle_payment(db, payment, line)
        return Verdict(
            Classification.HEALED if healed else Classification.MATCHED,
            **base,
            expected=payment.amount,
            actual=line.gross,
            posted=posted,
        )

    async def _settle_payment(
        self, db: AsyncSession, payment: Payment, line: SettlementLine
    ) -> int:
        wallet, system = await payment_accounts(db, payment)
        posted = 0
        settlement = await post_transfer(
            db,
            tenant_id=payment.tenant_id,
            reference=Reference(posting_rules.SETTLEMENT_CHARGE, "payment", payment.id),
            lines=posting_rules.settlement_charge(line.gross, line.fee, system),
        )
        posted += settlement.created
        active_refund = (
            await db.execute(
                select(Refund.id).where(
                    Refund.payment_id == payment.id, Refund.status.in_(ACTIVE_REFUNDS)
                )
            )
        ).first()
        # The money is released only if it is still in pending: a refund
        # (active or done) already moved it out, and a failed refund returns
        # its hold to available once the payment is SETTLED.
        if payment.status is PaymentStatus.CAPTURED and active_refund is None:
            release = await post_transfer(
                db,
                tenant_id=payment.tenant_id,
                reference=Reference(posting_rules.RELEASE, "payment", payment.id),
                lines=posting_rules.release(payment.amount, wallet),
            )
            posted += release.created
        if payment.status is PaymentStatus.CAPTURED:
            payment.status = PaymentStatus.SETTLED
        payment.settled_at = self.clock.now()
        payment.settlement_line_id = line.id
        await db.flush()
        return posted

    async def _find_refund(
        self, db: AsyncSession, line: SettlementLine
    ) -> tuple[Payment, Refund] | None:
        found = (
            await db.execute(
                select(Refund).where(Refund.psp_reference == line.psp_reference)
            )
        ).scalar_one_or_none()
        if found is None and (refund_id := _uuid(line.merchant_reference)):
            found = await db.get(Refund, refund_id)
        if found is None:
            return None
        return await lock_refund(db, tenant_id=found.tenant_id, refund_id=found.id)

    async def _refund_line(self, db: AsyncSession, line: SettlementLine) -> Verdict:
        located = await self._find_refund(db, line)
        if located is None:
            return Verdict(
                Classification.MISSING_IN_LEDGER,
                actual=line.gross,
                detail=f"no refund for {line.psp_reference} / "
                f"{line.merchant_reference}",
            )
        payment, refund = located
        base = {
            "tenant_id": refund.tenant_id,
            "payment_id": payment.id,
            "refund_id": refund.id,
        }
        if refund.psp_reference not in (None, line.psp_reference):
            return Verdict(
                Classification.STATUS_CONFLICT,
                **base,
                detail=f"recorded refund {refund.psp_reference}, "
                f"line {line.psp_reference}",
            )
        problem = _amount_problem(line, refund.amount, refund.currency)
        if problem:
            return Verdict(
                Classification.AMOUNT_MISMATCH,
                **base,
                expected=refund.amount,
                actual=line.gross,
                detail=problem,
            )
        if refund.settlement_line_id == line.id:
            return Verdict(
                Classification.MATCHED, **base, detail="already settled by this line"
            )
        if refund.settlement_line_id is not None:
            return Verdict(
                Classification.DUPLICATE_LINE,
                **base,
                actual=line.gross,
                detail=f"already settled by line {refund.settlement_line_id}",
            )
        healed = False
        if refund.status is RefundStatus.PROCESSING:
            try:
                await apply_refund_result(
                    db,
                    tenant_id=refund.tenant_id,
                    refund_id=refund.id,
                    outcome=RefundOutcome(
                        RefundOutcomeKind.SUCCEEDED, line.psp_reference
                    ),
                    source=SOURCE,
                )
            except ConflictingOutcomeError as exc:  # pragma: no cover - status checked
                return Verdict(Classification.STATUS_CONFLICT, **base, detail=str(exc))
            healed = True
        if refund.status is not RefundStatus.SUCCEEDED:
            return Verdict(
                Classification.STATUS_CONFLICT,
                **base,
                expected=0,
                actual=line.gross,
                detail=f"provider settled a refund we recorded as {refund.status.name}",
            )
        _, system = await payment_accounts(db, payment)
        posted = await post_transfer(
            db,
            tenant_id=refund.tenant_id,
            reference=Reference(posting_rules.SETTLEMENT_REFUND, "refund", refund.id),
            lines=posting_rules.settlement_refund(refund.amount, system),
        )
        refund.settlement_line_id = line.id
        await db.flush()
        return Verdict(
            Classification.HEALED if healed else Classification.MATCHED,
            **base,
            expected=refund.amount,
            actual=line.gross,
            posted=int(posted.created),
        )

    # --- what the provider never reported --------------------------------------

    async def find_missing_at_psp(
        self, run_id: uuid.UUID, file_date: date
    ) -> list[Verdict]:
        """Captures and confirmed refunds dated (file date - window) that no
        loaded line mentions. Checked once per date, by the run that closes
        its window; re-running repeats the same findings."""
        check_date = file_date - timedelta(
            days=self.settings.RECON_SETTLEMENT_WINDOW_DAYS
        )
        params = {"provider": self.provider, "day": check_date}
        async with self.session_factory() as db:
            payments = (
                await db.execute(
                    text(
                        """
                        SELECT p.id, p.tenant_id, p.amount FROM payments p
                        JOIN transfers t ON t.tenant_id = p.tenant_id
                         AND t.kind = 'capture' AND t.reference_type = 'payment'
                         AND t.reference_id = p.id
                        WHERE p.settlement_line_id IS NULL
                          AND p.status IN ('CAPTURED', 'SETTLED', 'REFUNDED')
                          AND (t.created_at AT TIME ZONE 'UTC')::date = :day
                          AND NOT EXISTS (
                            SELECT 1 FROM settlement_lines l
                            WHERE l.provider = :provider AND l.type = 'charge'
                              AND (l.psp_reference = p.psp_reference
                                   OR l.merchant_reference = p.id::text))
                        ORDER BY p.id
                        """
                    ),
                    params,
                )
            ).all()
            refunds = (
                await db.execute(
                    text(
                        """
                        SELECT r.id, r.tenant_id, r.payment_id, r.amount FROM refunds r
                        JOIN transfers t ON t.tenant_id = r.tenant_id
                         AND t.kind = 'refund_confirm' AND t.reference_type = 'refund'
                         AND t.reference_id = r.id
                        WHERE r.settlement_line_id IS NULL AND r.status = 'SUCCEEDED'
                          AND (t.created_at AT TIME ZONE 'UTC')::date = :day
                          AND NOT EXISTS (
                            SELECT 1 FROM settlement_lines l
                            WHERE l.provider = :provider AND l.type = 'refund'
                              AND (l.psp_reference = r.psp_reference
                                   OR l.merchant_reference = r.id::text))
                        ORDER BY r.id
                        """
                    ),
                    params,
                )
            ).all()
            verdicts = [
                Verdict(
                    Classification.MISSING_AT_PSP,
                    tenant_id=p.tenant_id,
                    payment_id=p.id,
                    expected=p.amount,
                    detail=f"capture on {check_date} not in any settlement file",
                )
                for p in payments
            ] + [
                Verdict(
                    Classification.MISSING_AT_PSP,
                    tenant_id=r.tenant_id,
                    payment_id=r.payment_id,
                    refund_id=r.id,
                    expected=r.amount,
                    detail=f"refund on {check_date} not in any settlement file",
                )
                for r in refunds
            ]
            for verdict in verdicts:
                db.add(
                    ReconItem(
                        id=uuid.uuid4(),
                        run_id=run_id,
                        classification=verdict.classification,
                        tenant_id=verdict.tenant_id,
                        payment_id=verdict.payment_id,
                        refund_id=verdict.refund_id,
                        expected=verdict.expected,
                        detail=verdict.detail,
                        created_at=self.clock.now(),
                    )
                )
            await db.commit()
        return verdicts
