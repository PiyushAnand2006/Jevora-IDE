"""SQLAlchemy ORM models (data model from the master spec §4)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.core.ids import new_id, utcnow
from backend.db.base import Base, UTCDateTime


def _id(prefix: str) -> Any:
    return lambda: new_id(prefix)


# --------------------------------------------------------------------------- users / auth
class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id("usr_"))
    email: Mapped[str | None] = mapped_column(String(320), unique=True, nullable=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str | None] = mapped_column(String(300), nullable=True)
    is_local: Mapped[bool] = mapped_column(Boolean, default=False)
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class OAuthAccount(Base):
    __tablename__ = "oauth_accounts"
    __table_args__ = (UniqueConstraint("provider", "provider_user_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id("oa_"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    provider_user_id: Mapped[str] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id("rt_"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    family_id: Mapped[str] = mapped_column(String(64), index=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)


class PasswordResetChallenge(Base):
    """One-time email OTP challenge used to authorize a password reset."""

    __tablename__ = "password_reset_challenges"
    __table_args__ = (Index("ix_password_reset_email_created", "email", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id("pwd_"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    code_salt: Mapped[str] = mapped_column(String(128))
    code_hash: Mapped[str] = mapped_column(String(128))
    reset_token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# --------------------------------------------------------------------------- providers / routing
class Provider(Base):
    __tablename__ = "providers"
    __table_args__ = (UniqueConstraint("user_id", "name"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id("prv_"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    base_url: Mapped[str] = mapped_column(String(500))
    kind: Mapped[str] = mapped_column(String(20))
    encrypted_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    auto_discovered: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    bindings: Mapped[list[ModelBinding]] = relationship(
        back_populates="provider", cascade="all, delete-orphan", passive_deletes=True
    )


class ModelBinding(Base):
    __tablename__ = "model_bindings"
    __table_args__ = (UniqueConstraint("provider_id", "model"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id("mb_"))
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"), index=True)
    model: Mapped[str] = mapped_column(String(300))
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    tier: Mapped[str] = mapped_column(String(32), default="balanced")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    provider: Mapped[Provider] = relationship(back_populates="bindings")


class RouterConfig(Base):
    __tablename__ = "router_configs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id("rc_"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    mapping: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


# --------------------------------------------------------------------------- agents
class AgentTemplate(Base):
    __tablename__ = "agent_templates"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    builtin: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Agent(Base):
    __tablename__ = "agents"
    __table_args__ = (UniqueConstraint("user_id", "name"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id("agt_"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    template: Mapped[str | None] = mapped_column(String(64), nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    generated_by: Mapped[str] = mapped_column(String(32), default="user")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class AgentMemory(Base):
    __tablename__ = "agent_memories"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id("am_"))
    agent_key: Mapped[str] = mapped_column(String(120), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id("ws_"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    root_path: Mapped[str] = mapped_column(String(1000))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class WorkspaceMemory(Base):
    __tablename__ = "workspace_memories"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id("wm_"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# --------------------------------------------------------------------------- conversations
class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id("cnv_"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    mode: Mapped[str] = mapped_column(String(20), default="chat")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id("msg_"))
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# --------------------------------------------------------------------------- runs
class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (Index("ix_runs_user_created", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id("run_"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    workspace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mode: Mapped[str] = mapped_column(String(20))
    task: Mapped[str] = mapped_column(Text)
    command: Mapped[str | None] = mapped_column(String(40), nullable=True)
    model_override: Mapped[str | None] = mapped_column(String(300), nullable=True)
    attachments: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    final_output: Mapped[str] = mapped_column(Text, default="")
    iteration_count: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_estimate: Mapped[float] = mapped_column(Float, default=0.0)
    vault_note_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AgentCall(Base):
    """The parent→child chain of every agent/subagent invocation within a run."""

    __tablename__ = "agent_calls"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id("call_"))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    parent_call_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_calls.id", ondelete="CASCADE"), nullable=True, index=True
    )
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_name: Mapped[str] = mapped_column(String(120), default="")
    role: Mapped[str] = mapped_column(String(40))
    model_used: Mapped[str | None] = mapped_column(String(300), nullable=True)
    provider_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    depth: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="running")
    input: Mapped[str] = mapped_column(Text, default="")
    output: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    lineage: Mapped[list[str]] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Decision(Base):
    """Every decision-layer verdict, with the state it saw (training/eval log)."""

    __tablename__ = "decisions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id("dec_"))
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    type: Mapped[str] = mapped_column(String(32), index=True)
    engine: Mapped[str] = mapped_column(String(40))
    input_state_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evidence_ref: Mapped[list[str]] = mapped_column(JSON, default=list)
    choice: Mapped[str] = mapped_column(String(60))
    confidence: Mapped[float] = mapped_column(Float)
    alternatives: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False)
    shadow: Mapped[bool] = mapped_column(Boolean, default=False)
    acted: Mapped[bool] = mapped_column(Boolean, default=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # Added in migration 0002 (labelling for the fine-tuning loop).
    label: Mapped[str | None] = mapped_column(String(60), nullable=True)
    label_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    labelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id("art_"))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(32))
    path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id("att_"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(300))
    mime: Mapped[str] = mapped_column(String(120))
    size: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20))
    storage_path: Mapped[str] = mapped_column(String(1000))
    text_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class MCPServer(Base):
    __tablename__ = "mcp_servers"
    __table_args__ = (UniqueConstraint("user_id", "name"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id("mcp_"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    transport: Mapped[str] = mapped_column(String(20))
    command: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    args: Mapped[list[str]] = mapped_column(JSON, default=list)
    encrypted_env: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    encrypted_headers: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_status: Mapped[str] = mapped_column(String(20), default="unknown")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class PluginState(Base):
    __tablename__ = "plugin_states"
    __table_args__ = (UniqueConstraint("user_id", "plugin_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id("pls_"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    plugin_id: Mapped[str] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class VM(Base):
    """A sandbox environment (container) a user can create, select and stop."""

    __tablename__ = "vms"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id("vm_"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="stopped")
    container_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    image: Mapped[str | None] = mapped_column(String(300), nullable=True)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    network: Mapped[bool] = mapped_column(Boolean, default=False)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class EscalationRequest(Base):
    __tablename__ = "escalation_requests"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id("esc_"))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(20), default="decision")
    question: Mapped[str] = mapped_column(Text)
    options: Mapped[list[str]] = mapped_column(JSON, default=list)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="open")
    resolved_choice: Mapped[str | None] = mapped_column(String(60), nullable=True)
    resolved_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class SettingKV(Base):
    """Small global key/value store for runtime-editable configuration."""

    __tablename__ = "settings_kv"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class UserSetting(Base):
    """Settings owned by one authenticated user, never shared globally."""

    __tablename__ = "user_settings"
    __table_args__ = (UniqueConstraint("user_id", "key"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id("ust_"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(200))
    value: Mapped[Any] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class LLMCall(Base):
    """Token + cost accounting per provider call (added in migration 0002)."""

    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_calls_user_created", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id("llm_"))
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    model: Mapped[str] = mapped_column(String(300))
    category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(20), default="ok")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
