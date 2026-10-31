"""Browser-control sessions: DCOM/desktop/Playwright sessions bound to users and VMs.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-31 00:00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import backend.db.base


revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "browser_sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("vm_id", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("browser_path", sa.String(length=500), nullable=True),
        sa.Column("clsid", sa.String(length=300), nullable=True),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("host", sa.String(length=200), nullable=True),
        sa.Column("ws_endpoint", sa.String(length=500), nullable=True),
        sa.Column("headless", sa.Boolean(), nullable=False),
        sa.Column("viewport_width", sa.Integer(), nullable=False),
        sa.Column("viewport_height", sa.Integer(), nullable=False),
        sa.Column("allowed_hosts", sa.JSON(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("started_at", backend.db.base.UTCDateTime(), nullable=True),
        sa.Column("last_activity_at", backend.db.base.UTCDateTime(), nullable=True),
        sa.Column("created_at", backend.db.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_browser_sessions_user_id_users"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["vm_id"], ["vms.id"], name=op.f("fk_browser_sessions_vm_id_vms"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_browser_sessions")),
    )
    with op.batch_alter_table("browser_sessions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_browser_sessions_user_id"), ["user_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_browser_sessions_vm_id"), ["vm_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("browser_sessions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_browser_sessions_vm_id"))
        batch_op.drop_index(batch_op.f("ix_browser_sessions_user_id"))
    op.drop_table("browser_sessions")
