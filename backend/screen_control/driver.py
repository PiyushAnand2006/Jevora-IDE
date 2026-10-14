"""Screen-driver contract and the action/assertion types shared by drivers and computer-use models."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel

from backend.decision.schemas import Assertion

ActionName = Literal["click", "double_click", "type", "scroll", "key", "wait", "navigate", "done"]


class Action(BaseModel):
    action: ActionName
    x: int | None = None
    y: int | None = None
    text: str | None = None
    dx: int = 0
    dy: int = 0
    key: str | None = None
    ms: int | None = None
    url: str | None = None
    reason: str = ""

    def signature(self) -> str:
        return f"{self.action}:{self.x}:{self.y}:{self.text}:{self.key}:{self.dx}:{self.dy}:{self.url}"


class AssertionSpec(BaseModel):
    type: Literal["text_visible", "title_contains", "url_contains", "selector_visible", "no_console_errors"]
    value: str = ""

    @property
    def name(self) -> str:
        return f"{self.type}({self.value})" if self.value else self.type


class DomainNotAllowed(Exception):
    pass


def host_allowed(url: str, allowed: list[str]) -> bool:
    parsed = urlparse(url)
    if parsed.scheme in ("about", "data"):
        return True
    host = (parsed.hostname or "").lower()
    return any(host == d.lower() or host.endswith("." + d.lower()) for d in allowed)


class ScreenDriver(ABC):
    kind: str = "abstract"
    width: int = 1280
    height: int = 800

    @abstractmethod
    async def start(self, url: str | None) -> None: ...

    @abstractmethod
    async def screenshot(self) -> bytes: ...

    @abstractmethod
    async def perform(self, action: Action) -> str: ...

    @abstractmethod
    async def current_url(self) -> str: ...

    @abstractmethod
    async def check(self, spec: AssertionSpec) -> Assertion: ...

    @abstractmethod
    def console_errors(self) -> list[str]: ...

    @abstractmethod
    def network_errors(self) -> list[str]: ...

    @abstractmethod
    async def close(self) -> None: ...
