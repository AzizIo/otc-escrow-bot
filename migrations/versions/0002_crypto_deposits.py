"""Add Crypto Pay deposit invoices."""

import sqlalchemy as sa
from alembic import op

revision = "0002_crypto_deposits"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "crypto_deposits",
        sa.Column("invoice_id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("amount > 0", name="ck_crypto_deposits_amount_positive"),
    )
    op.create_index("ix_crypto_deposits_user_id", "crypto_deposits", ["user_id"])
    op.create_index("ix_crypto_deposits_status", "crypto_deposits", ["status"])


def downgrade() -> None:
    op.drop_index("ix_crypto_deposits_status", table_name="crypto_deposits")
    op.drop_index("ix_crypto_deposits_user_id", table_name="crypto_deposits")
    op.drop_table("crypto_deposits")
