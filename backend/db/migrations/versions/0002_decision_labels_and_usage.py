"""Decision labelling (fine-tuning loop) and per-call token/cost accounting

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-02 21:00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import backend.db.base

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("decisions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("label", sa.String(length=60), nullable=True))
        batch_op.add_column(sa.Column("label_note", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("labelled_at", backend.db.base.UTCDateTime(), nullable=True))

    op.create_table(
        "llm_calls",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=True),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("call_id", sa.String(length=64), nullable=True),
        sa.Column("provider_id", sa.String(length=64), nullable=True),
        sa.Column("provider_kind", sa.String(length=20), nullable=True),
        sa.Column("model", sa.String(length=300), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", backend.db.base.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_calls")),
    )
    with op.batch_alter_table("llm_calls", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_llm_calls_run_id"), ["run_id"], unique=False)
        batch_op.create_index("ix_llm_calls_user_created", ["user_id", "created_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("llm_calls", schema=None) as batch_op:
        batch_op.drop_index("ix_llm_calls_user_created")
        batch_op.drop_index(batch_op.f("ix_llm_calls_run_id"))
    op.drop_table("llm_calls")
    with op.batch_alter_table("decisions", schema=None) as batch_op:
        batch_op.drop_column("labelled_at")
        batch_op.drop_column("label_note")
        batch_op.drop_column("label")
