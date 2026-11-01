"""Scheduled agent tasks: recurring runs created by a background scheduler.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-01 00:00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import backend.db.base


revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scheduled_tasks",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("interval_minutes", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("conversation_id", sa.String(length=64), nullable=True),
        sa.Column("workspace_id", sa.String(length=64), nullable=True),
        sa.Column("last_run_id", sa.String(length=64), nullable=True),
        sa.Column("next_run_at", backend.db.base.UTCDateTime(), nullable=False),
        sa.Column("created_at", backend.db.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_scheduled_tasks_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scheduled_tasks")),
    )
    with op.batch_alter_table("scheduled_tasks", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_scheduled_tasks_user_id"), ["user_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("scheduled_tasks", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_scheduled_tasks_user_id"))
    op.drop_table("scheduled_tasks")
