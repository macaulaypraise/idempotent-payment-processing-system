"""stage 6: settlement lines, reconciliation runs and items

Revision ID: b9d5e3f1a274
Revises: a7f3c2d8e190
Create Date: 2026-11-21

- settlement_lines: the provider's daily file as loaded. Unique
  (provider, file_date, line_id), so loading a file twice adds nothing.
- recon_runs / recon_items: one run per reconciliation of a date, one item
  per line (and per capture or refund the provider never reported).
- payments.settlement_line_id / refunds.settlement_line_id: the line that
  settled the row. Set once; a trigger refuses to change it, so a second line
  for the same charge can only be reported as a duplicate, never re-posted.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b9d5e3f1a274"
down_revision: str | None = "a7f3c2d8e190"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CLASSIFICATIONS = (
    "matched",
    "healed",
    "amount_mismatch",
    "missing_in_ledger",
    "duplicate_line",
    "missing_at_psp",
    "status_conflict",
)
RUN_STATUSES = ("running", "completed", "failed", "abandoned")


def _in(values: Sequence[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _run(*statements: str) -> None:
    for statement in statements:
        op.execute(statement)


def upgrade() -> None:
    op.create_table(
        "settlement_lines",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("file_date", sa.Date(), nullable=False),
        sa.Column("line_id", sa.String(length=128), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("psp_reference", sa.String(length=128), nullable=False),
        sa.Column("merchant_reference", sa.String(length=128), nullable=True),
        sa.Column("gross", sa.BigInteger(), nullable=False),
        sa.Column("fee", sa.BigInteger(), nullable=False),
        sa.Column("net", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "loaded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Shape only. Whether the amounts agree with the ledger is the
        # reconciliation's question, so they are not constrained here.
        sa.CheckConstraint("type IN ('charge', 'refund')", name="ck_settlement_type"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", "file_date", "line_id", name="uq_settlement_lines_line"
        ),
    )
    op.create_index(
        "ix_settlement_lines_psp_reference", "settlement_lines", ["psp_reference"]
    )
    op.create_index(
        "ix_settlement_lines_merchant_reference",
        "settlement_lines",
        ["merchant_reference"],
    )

    op.create_table(
        "recon_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("file_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("lines", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "counts",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("unexplained", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("posted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"status IN ({_in(RUN_STATUSES)})", name="ck_recon_runs_status"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_recon_runs_date", "recon_runs", ["provider", "file_date"])

    op.create_table(
        "recon_items",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("classification", sa.String(length=24), nullable=False),
        sa.Column("settlement_line_id", sa.UUID(), nullable=True),
        sa.Column("tenant_id", sa.UUID(), nullable=True),
        sa.Column("payment_id", sa.UUID(), nullable=True),
        sa.Column("refund_id", sa.UUID(), nullable=True),
        sa.Column("expected", sa.BigInteger(), nullable=True),
        sa.Column("actual", sa.BigInteger(), nullable=True),
        sa.Column("posted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            f"classification IN ({_in(CLASSIFICATIONS)})",
            name="ck_recon_items_classification",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["recon_runs.id"]),
        sa.ForeignKeyConstraint(["settlement_line_id"], ["settlement_lines.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"]),
        sa.ForeignKeyConstraint(["refund_id"], ["refunds.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_recon_items_run", "recon_items", ["run_id", "classification"])
    # A line is classified once per run, even if the run is retried in part
    op.create_index(
        "uq_recon_items_run_line",
        "recon_items",
        ["run_id", "settlement_line_id"],
        unique=True,
        postgresql_where=sa.text("settlement_line_id IS NOT NULL"),
    )

    for table in ("payments", "refunds"):
        op.add_column(
            table,
            sa.Column(
                "settlement_line_id",
                sa.UUID(),
                sa.ForeignKey(
                    "settlement_lines.id", name=f"fk_{table}_settlement_line"
                ),
                nullable=True,
            ),
        )
    _run(
        """
        CREATE FUNCTION settlement_line_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.settlement_line_id IS NOT NULL
               AND NEW.settlement_line_id IS DISTINCT FROM OLD.settlement_line_id THEN
                RAISE EXCEPTION '% %: settlement line is set once',
                    TG_TABLE_NAME, OLD.id USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$
        """,
        "CREATE TRIGGER payments_settlement_line_guard "
        "BEFORE UPDATE OF settlement_line_id ON payments "
        "FOR EACH ROW EXECUTE FUNCTION settlement_line_guard()",
        "CREATE TRIGGER refunds_settlement_line_guard "
        "BEFORE UPDATE OF settlement_line_id ON refunds "
        "FOR EACH ROW EXECUTE FUNCTION settlement_line_guard()",
    )


def downgrade() -> None:
    _run(
        "DROP TRIGGER IF EXISTS refunds_settlement_line_guard ON refunds",
        "DROP TRIGGER IF EXISTS payments_settlement_line_guard ON payments",
        "DROP FUNCTION IF EXISTS settlement_line_guard()",
    )
    for table in ("refunds", "payments"):
        op.drop_column(table, "settlement_line_id")
    op.drop_index("uq_recon_items_run_line", table_name="recon_items")
    op.drop_index("ix_recon_items_run", table_name="recon_items")
    op.drop_table("recon_items")
    op.drop_index("ix_recon_runs_date", table_name="recon_runs")
    op.drop_table("recon_runs")
    op.drop_index(
        "ix_settlement_lines_merchant_reference", table_name="settlement_lines"
    )
    op.drop_index("ix_settlement_lines_psp_reference", table_name="settlement_lines")
    op.drop_table("settlement_lines")
