"""stage 2: double-entry ledger, wallets, app role, integer money

Revision ID: c3a8d1e6f204
Revises: b2e1c4a7d9f0
Create Date: 2026-11-21

ADR 0002 (ledger model) and ADR 0003 (atomic sorted total updates).

Rules enforced by the database, not only the application:
- every transfer balances per currency and has at least two entries
  (deferred constraint triggers, checked at COMMIT)
- entries and transfers are append-only (triggers reject UPDATE, DELETE and
  TRUNCATE for every role, the owner included)
- guarded accounts (wallet pending/available) cannot go negative (CHECK)
- an entry's currency must match its account's currency (composite FK)
- the application role has no UPDATE/DELETE/TRUNCATE on entries/transfers

Also: payments.amount Numeric(12,2) -> BIGINT minor units; drop payments.version.
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "c3a8d1e6f204"
down_revision: str | None = "b2e1c4a7d9f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _app_role() -> str:
    from app.config import get_settings

    role = str(context.config.attributes.get("app_role") or get_settings().APP_DB_ROLE)
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", role):
        raise ValueError(f"invalid APP_DB_ROLE {role!r}")
    return role


def _run(*statements: str) -> None:
    """asyncpg executes one statement per call."""
    for statement in statements:
        op.execute(statement)


ACCOUNT_KINDS = (
    "wallet_pending",
    "wallet_available",
    "psp_clearing",
    "bank",
    "psp_fees",
    "refunds_payable",
)


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("normal_side", sa.String(length=6), nullable=False),
        sa.Column("debits_posted", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "credits_posted", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("guarded", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("wallet_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "kind IN (" + ", ".join(f"'{k}'" for k in ACCOUNT_KINDS) + ")",
            name="ck_accounts_kind",
        ),
        sa.CheckConstraint(
            "normal_side IN ('debit', 'credit')", name="ck_accounts_normal_side"
        ),
        sa.CheckConstraint(
            "debits_posted >= 0 AND credits_posted >= 0", name="ck_accounts_totals"
        ),
        sa.CheckConstraint(
            "NOT guarded OR debits_posted <= credits_posted", name="ck_accounts_guard"
        ),
        sa.CheckConstraint(
            "(wallet_id IS NULL) = "
            "(kind NOT IN ('wallet_pending', 'wallet_available'))",
            name="ck_accounts_wallet_kinds",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "currency", name="uq_accounts_id_currency"),
    )
    # One system account of each kind per tenant and currency.
    op.create_index(
        "uq_accounts_system_kind",
        "accounts",
        ["tenant_id", "kind", "currency"],
        unique=True,
        postgresql_where=sa.text("wallet_id IS NULL"),
    )
    op.create_index(
        "uq_accounts_wallet_kind",
        "accounts",
        ["wallet_id", "kind"],
        unique=True,
        postgresql_where=sa.text("wallet_id IS NOT NULL"),
    )

    op.create_table(
        "wallets",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("pending_account_id", sa.UUID(), nullable=False),
        sa.Column("available_account_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(
            ["pending_account_id", "currency"], ["accounts.id", "accounts.currency"]
        ),
        sa.ForeignKeyConstraint(
            ["available_account_id", "currency"], ["accounts.id", "accounts.currency"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pending_account_id"),
        sa.UniqueConstraint("available_account_id"),
    )
    op.create_index("ix_wallets_tenant_id", "wallets", ["tenant_id"])

    op.create_table(
        "transfers",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("reference_type", sa.String(length=32), nullable=False),
        sa.Column("reference_id", sa.UUID(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "kind",
            "reference_type",
            "reference_id",
            name="uq_transfers_reference",
        ),
    )

    op.create_table(
        "entries",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("transfer_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("direction", sa.String(length=6), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("amount > 0", name="ck_entries_amount_positive"),
        sa.CheckConstraint(
            "direction IN ('debit', 'credit')", name="ck_entries_direction"
        ),
        sa.ForeignKeyConstraint(["transfer_id"], ["transfers.id"]),
        sa.ForeignKeyConstraint(
            ["account_id", "currency"],
            ["accounts.id", "accounts.currency"],
            name="fk_entries_account_currency",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_entries_transfer_id", "entries", ["transfer_id"])
    op.create_index("ix_entries_account_id_id", "entries", ["account_id", "id"])

    # --- Balance: every transfer nets to zero per currency, >= 2 entries ------
    _run(
        """
        CREATE FUNCTION ledger_assert_transfer_balanced(p_transfer uuid)
        RETURNS void LANGUAGE plpgsql AS $$
        DECLARE
            v_lines integer;
            v_bad text;
        BEGIN
            SELECT count(*) INTO v_lines FROM entries WHERE transfer_id = p_transfer;
            IF v_lines < 2 THEN
                RAISE EXCEPTION 'transfer % has % entries; at least 2 required',
                    p_transfer, v_lines USING ERRCODE = 'check_violation';
            END IF;
            SELECT currency INTO v_bad
            FROM entries WHERE transfer_id = p_transfer
            GROUP BY currency
            HAVING sum(CASE WHEN direction = 'debit' THEN amount ELSE -amount END) <> 0
            LIMIT 1;
            IF FOUND THEN
                RAISE EXCEPTION 'transfer % is unbalanced in %', p_transfer, v_bad
                    USING ERRCODE = 'check_violation';
            END IF;
        END $$
        """,
        """
        CREATE FUNCTION ledger_check_entry_transfer() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM ledger_assert_transfer_balanced(NEW.transfer_id);
            RETURN NULL;
        END $$
        """,
        """
        CREATE FUNCTION ledger_check_transfer() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM ledger_assert_transfer_balanced(NEW.id);
            RETURN NULL;
        END $$
        """,
        """
        CREATE CONSTRAINT TRIGGER entries_transfer_balanced
            AFTER INSERT ON entries DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION ledger_check_entry_transfer()
        """,
        """
        CREATE CONSTRAINT TRIGGER transfers_have_entries
            AFTER INSERT ON transfers DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION ledger_check_transfer()
        """,
    )

    # --- Immutability: append-only for every role, owner included ---------------
    _run(
        """
        CREATE FUNCTION ledger_reject_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% on % is not allowed: the ledger is append-only',
                TG_OP, TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege';
        END $$
        """
    )
    for table in ("entries", "transfers"):
        _run(
            f"CREATE TRIGGER {table}_no_update_delete BEFORE UPDATE OR DELETE "
            f"ON {table} FOR EACH ROW EXECUTE FUNCTION ledger_reject_mutation()",
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE "
            f"ON {table} FOR EACH STATEMENT EXECUTE FUNCTION ledger_reject_mutation()",
        )

    # --- Integer money on payments -----------------------------------------------
    op.add_column("payments", sa.Column("amount_minor", sa.BigInteger(), nullable=True))
    # v1 stored exactly two decimal places, so x100 is exact.
    op.execute("UPDATE payments SET amount_minor = (amount * 100)::bigint")
    op.alter_column("payments", "amount_minor", nullable=False)
    op.drop_column("payments", "amount")
    op.alter_column("payments", "amount_minor", new_column_name="amount")
    op.create_check_constraint("ck_payments_amount_positive", "payments", "amount > 0")
    op.drop_column("payments", "version")

    # --- Application role -----------------------------------------------------------
    role = _app_role()
    _run(
        f"""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                CREATE ROLE {role} NOLOGIN;
            END IF;
        END $$
        """,
        f"GRANT USAGE ON SCHEMA public TO {role}",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
        f"TO {role}",
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}",
        f"REVOKE ALL ON alembic_version FROM {role}",
        f"REVOKE UPDATE, DELETE, TRUNCATE ON entries, transfers FROM {role}",
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}",
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT USAGE, SELECT ON SEQUENCES TO {role}",
    )


def downgrade() -> None:
    role = _app_role()
    _run(
        f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                ALTER DEFAULT PRIVILEGES IN SCHEMA public
                    REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {role};
                ALTER DEFAULT PRIVILEGES IN SCHEMA public
                    REVOKE USAGE, SELECT ON SEQUENCES FROM {role};
                REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role};
                REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {role};
                REVOKE USAGE ON SCHEMA public FROM {role};
            END IF;
        END $$
        """
    )

    op.add_column(
        "payments",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.drop_constraint("ck_payments_amount_positive", "payments", type_="check")
    op.add_column(
        "payments",
        sa.Column("amount_major", sa.Numeric(precision=12, scale=2), nullable=True),
    )
    op.execute("UPDATE payments SET amount_major = amount / 100.0")
    op.alter_column("payments", "amount_major", nullable=False)
    op.drop_column("payments", "amount")
    op.alter_column("payments", "amount_major", new_column_name="amount")

    op.drop_table("entries")
    op.drop_table("transfers")
    op.drop_index("ix_wallets_tenant_id", table_name="wallets")
    op.drop_table("wallets")
    op.drop_index("uq_accounts_wallet_kind", table_name="accounts")
    op.drop_index("uq_accounts_system_kind", table_name="accounts")
    op.drop_table("accounts")
    _run(
        "DROP FUNCTION IF EXISTS ledger_reject_mutation()",
        "DROP FUNCTION IF EXISTS ledger_check_transfer()",
        "DROP FUNCTION IF EXISTS ledger_check_entry_transfer()",
        "DROP FUNCTION IF EXISTS ledger_assert_transfer_balanced(uuid)",
    )
