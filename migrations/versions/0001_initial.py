"""Initial schema."""

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(64)),
        sa.Column("full_name", sa.String(160)),
        sa.Column("is_blocked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("telegram_id"),
    )
    op.create_index("ix_users_telegram_id", "users", ["telegram_id"])

    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, unique=True),
        sa.Column("available", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("frozen", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(8), nullable=False, server_default="USDT"),
    )

    op.create_table(
        "deals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("buyer_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("seller_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="created"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_deals_buyer_id", "deals", ["buyer_id"])
    op.create_index("ix_deals_seller_id", "deals", ["seller_id"])

    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("available_delta", sa.Numeric(18, 2), nullable=False),
        sa.Column("frozen_delta", sa.Numeric(18, 2), nullable=False),
        sa.Column("actor_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("deal_id", sa.Integer(), sa.ForeignKey("deals.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_ledger_entries_account_id", "ledger_entries", ["account_id"])
    op.execute("""
        CREATE FUNCTION prevent_ledger_mutation() RETURNS trigger
        LANGUAGE plpgsql
        AS 'BEGIN
            RAISE EXCEPTION ''ledger_entries are append-only'';
        END;';
    """)
    op.execute("""
        CREATE TRIGGER ledger_entries_immutable
        BEFORE UPDATE OR DELETE ON ledger_entries
        FOR EACH ROW EXECUTE FUNCTION prevent_ledger_mutation();
    """)


def downgrade() -> None:
    op.drop_index("ix_ledger_entries_account_id", table_name="ledger_entries")
    op.execute("DROP TRIGGER IF EXISTS ledger_entries_immutable ON ledger_entries")
    op.execute("DROP FUNCTION IF EXISTS prevent_ledger_mutation()")
    op.drop_table("ledger_entries")
    op.drop_index("ix_deals_seller_id", table_name="deals")
    op.drop_index("ix_deals_buyer_id", table_name="deals")
    op.drop_table("deals")
    op.drop_table("accounts")
    op.drop_index("ix_users_telegram_id", table_name="users")
    op.drop_table("users")
