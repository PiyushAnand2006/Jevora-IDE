"""Conversations belong to a project (workspace) for sidebar grouping.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-03 00:00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.add_column(sa.Column("project_id", sa.String(length=64), nullable=True))
        batch_op.create_index(batch_op.f("ix_conversations_project_id"), ["project_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_conversations_project_id"))
        batch_op.drop_column("project_id")
