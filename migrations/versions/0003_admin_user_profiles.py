"""Add admin-managed profile display values and audit history."""

import sqlalchemy as sa
from alembic import op

revision = "0003_admin_user_profiles"
down_revision = "0002_crypto_deposits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("profile_deals_override", sa.Integer(), nullable=True))
    op.add_column("users", sa.Column("profile_successful_override", sa.Integer(), nullable=True))
    op.add_column("users", sa.Column("profile_since_override", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_users_profile_deals_override_nonnegative",
        "users",
        "profile_deals_override IS NULL OR profile_deals_override >= 0",
    )
    op.create_check_constraint(
        "ck_users_profile_successful_override_nonnegative",
        "users",
        "profile_successful_override IS NULL OR profile_successful_override >= 0",
    )
    op.create_table(
        "user_admin_audit",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("actor_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("field", sa.String(48), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_user_admin_audit_user_id", "user_admin_audit", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_admin_audit_user_id", table_name="user_admin_audit")
    op.drop_table("user_admin_audit")
    op.drop_constraint("ck_users_profile_successful_override_nonnegative", "users", type_="check")
    op.drop_constraint("ck_users_profile_deals_override_nonnegative", "users", type_="check")
    op.drop_column("users", "profile_since_override")
    op.drop_column("users", "profile_successful_override")
    op.drop_column("users", "profile_deals_override")
