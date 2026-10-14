"""AgentConfig: system prompt + tool permissions + model binding + memory scope (+ limits)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

ToolPermission = Literal[
    "fs.read", "fs.write", "fs.search", "terminal.run", "tests.run", "git.read", "git.write",
    "subagent.spawn", "mcp.call", "web.search", "screen.control", "memory.write",
]
ALL_PERMISSIONS: tuple[str, ...] = (
    "fs.read", "fs.write", "fs.search", "terminal.run", "tests.run", "git.read", "git.write",
    "subagent.spawn", "mcp.call", "web.search", "screen.control", "memory.write",
)
MemoryScope = Literal["run", "agent", "workspace", "none"]
Category = Literal["heavy_reasoning", "fast_classification", "code_generation", "creative_ui",
                   "research_retrieval", "computer_use"]


class ModelBindingSpec(BaseModel):
    mode: Literal["auto", "explicit"] = "auto"
    category: Category | None = None
    provider_id: str | None = None
    model: str | None = None

    def spec(self) -> str:
        if self.mode == "explicit" and self.model:
            return f"{self.provider_id}:{self.model}" if self.provider_id else self.model
        return "auto"


class Budget(BaseModel):
    max_tokens: int = Field(default=250_000, ge=1_000)
    max_seconds: int = Field(default=900, ge=5)
    max_cost_usd: float = Field(default=2.0, ge=0.0)
    max_tool_calls: int = Field(default=80, ge=1)
    max_steps: int = Field(default=24, ge=1, le=200)

    def narrowed(self, parent: Budget) -> Budget:
        return Budget(max_tokens=min(self.max_tokens, parent.max_tokens),
                      max_seconds=min(self.max_seconds, parent.max_seconds),
                      max_cost_usd=min(self.max_cost_usd, parent.max_cost_usd),
                      max_tool_calls=min(self.max_tool_calls, parent.max_tool_calls),
                      max_steps=min(self.max_steps, parent.max_steps))


class AgentConfig(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=2000)
    template: str | None = None
    system_prompt: str = Field(min_length=1, max_length=20_000)
    tool_permissions: list[ToolPermission] = Field(default_factory=list)
    model_binding: ModelBindingSpec = Field(default_factory=ModelBindingSpec)
    memory_scope: MemoryScope = "run"
    max_depth: int = Field(default=4, ge=0, le=8)
    max_fanout: int = Field(default=5, ge=0, le=20)
    budget: Budget = Field(default_factory=Budget)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)

    @field_validator("tool_permissions")
    @classmethod
    def _dedupe(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))

    @property
    def category(self) -> str | None:
        return self.model_binding.category

    def has(self, permission: str) -> bool:
        return permission in self.tool_permissions

    def narrowed(self, parent: AgentConfig) -> AgentConfig:
        """A child may never exceed its parent: permissions intersect, limits take the minimum."""
        return self.model_copy(update={
            "tool_permissions": [p for p in self.tool_permissions if p in parent.tool_permissions],
            "max_depth": min(self.max_depth, max(0, parent.max_depth - 1)),
            "max_fanout": min(self.max_fanout, parent.max_fanout),
            "budget": self.budget.narrowed(parent.budget),
        })

    def public(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
