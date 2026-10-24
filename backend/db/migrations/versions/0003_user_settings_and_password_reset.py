"""Per-user settings and password reset OTP challenges.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-24 00:00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import backend.db.base


revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "password_reset_challenges",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("code_salt", sa.String(length=128), nullable=False),
        sa.Column("code_hash", sa.String(length=128), nullable=False),
        sa.Column("reset_token_hash", sa.String(length=128), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("expires_at", backend.db.base.UTCDateTime(), nullable=False),
        sa.Column("verified_at", backend.db.base.UTCDateTime(), nullable=True),
        sa.Column("consumed_at", backend.db.base.UTCDateTime(), nullable=True),
        sa.Column("created_at", backend.db.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_password_reset_challenges_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_password_reset_challenges")),
        sa.UniqueConstraint("reset_token_hash", name=op.f("uq_password_reset_challenges_reset_token_hash")),
    )
    with op.batch_alter_table("password_reset_challenges", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_password_reset_challenges_user_id"), ["user_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_password_reset_challenges_email"), ["email"], unique=False)
        batch_op.create_index("ix_password_reset_email_created", ["email", "created_at"], unique=False)

    op.create_table(
        "user_settings",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", backend.db.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_user_settings_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_settings")),
        sa.UniqueConstraint("user_id", "key", name=op.f("uq_user_settings_user_id")),
    )
    with op.batch_alter_table("user_settings", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_user_settings_user_id"), ["user_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("user_settings", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_user_settings_user_id"))
    op.drop_table("user_settings")

    with op.batch_alter_table("password_reset_challenges", schema=None) as batch_op:
        batch_op.drop_index("ix_password_reset_email_created")
        batch_op.drop_index(batch_op.f("ix_password_reset_challenges_email"))
        batch_op.drop_index(batch_op.f("ix_password_reset_challenges_user_id"))
    op.drop_table("password_reset_challenges")
