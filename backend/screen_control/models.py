"""Computer-use model adapters: screenshot in → one coordinate action out.

* ``OpenAIComputerUse``   — OpenAI ``computer-use-preview`` (Responses API, ``computer_use_preview`` tool)
* ``AnthropicComputerUse`` — Claude computer use (``computer_20250124`` tool, beta header)
* ``GeminiComputerUse``   — Gemini 2.5 Computer Use (``computerUse`` tool, 0–999 normalised coordinates)
* ``GenericVisionModel``  — any vision chat model via the provider router, asked for a JSON action

The binding comes from the router's ``computer_use`` category, so the vendor is configuration.
"""

from __future__ import annotations

import base64
import json
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

import httpx

from backend.providers.base import ChatMessage, ContentPart, ProviderError, classify_http_error
from backend.providers.jsonrepair import repair_json
from backend.screen_control.driver import Action

if TYPE_CHECKING:
    from backend.providers.router import Binding, CallMeta, ProviderRouter

GENERIC_PROMPT = (
    "You control a web browser to test an application. You see the current screenshot ({w}x{h} pixels; "
    "origin top-left). Choose exactly ONE next action to accomplish the goal and reply with ONLY a JSON object: "
    '{{"action": "click|double_click|type|scroll|key|wait|navigate|done", "x": int, "y": int, "text": str, '
    '"key": str, "dy": int, "url": str, "reason": str}}. Use pixel coordinates of the element centre. When the goal '
    'is achieved (or impossible), reply {{"action": "done", "reason": "..."}}.')


def _png_b64(png: bytes) -> str:
    return base64.b64encode(png).decode("ascii")


class ComputerUseModel(ABC):
    name = "abstract"
    model = ""

    @abstractmethod
    async def next_action(self, goal: str, screenshot: bytes, history: list[dict[str, Any]], width: int,
                          height: int) -> Action: ...


class GenericVisionModel(ComputerUseModel):
    name = "generic-vision"

    def __init__(self, router: ProviderRouter, meta: CallMeta, bindings: list[Binding]) -> None:
        self.router = router
        self.meta = meta
        self.bindings = bindings
        self.model = bindings[0].model if bindings else ""
        self.provider_id = bindings[0].provider_id if bindings else None

    async def next_action(self, goal: str, screenshot: bytes, history: list[dict[str, Any]], width: int,
                          height: int) -> Action:
        recent = "\n".join(f"step {i + 1}: {json.dumps(h['action'])} → {h.get('result', '')}"
                           for i, h in enumerate(history[-8:])) or "(no actions yet)"
        messages = [
            ChatMessage(role="system", content=GENERIC_PROMPT.format(w=width, h=height)),
            ChatMessage(role="user", content=[
                ContentPart(type="text", text=f"Goal: {goal}\nPrevious actions:\n{recent}"),
                ContentPart(type="image", image_b64=_png_b64(screenshot), mime="image/png")]),
        ]
        response = await self.router.chat(self.meta, messages, bindings=self.bindings,
                                          json_schema=Action.model_json_schema(), temperature=0.0)
        self.model = response.model
        self.provider_id = response.provider_id
        data = repair_json(response.content)
        return Action.model_validate(data)


class OpenAIComputerUse(ComputerUseModel):
    name = "openai-computer-use"

    def __init__(self, base_url: str, api_key: str | None, model: str, client: httpx.AsyncClient) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.client = client
        self._previous: str | None = None
        self._call_id: str | None = None
        self._safety: list[dict[str, Any]] = []

    def _tool(self, width: int, height: int) -> dict[str, Any]:
        return {"type": "computer_use_preview", "display_width": width, "display_height": height,
                "environment": "browser"}

    async def next_action(self, goal: str, screenshot: bytes, history: list[dict[str, Any]], width: int,
                          height: int) -> Action:
        image = f"data:image/png;base64,{_png_b64(screenshot)}"
        body: dict[str, Any] = {"model": self.model, "tools": [self._tool(width, height)], "truncation": "auto"}
        if self._previous and self._call_id:
            body["previous_response_id"] = self._previous
            body["input"] = [{"type": "computer_call_output", "call_id": self._call_id,
                              "output": {"type": "computer_screenshot", "image_url": image},
                              "acknowledged_safety_checks": self._safety}]
        else:
            body["input"] = [{"role": "user", "content": [{"type": "input_text", "text": goal},
                                                          {"type": "input_image", "image_url": image}]}]
        response = await self.client.post(f"{self.base_url}/responses", json=body, timeout=120,
                                          headers={"Authorization": f"Bearer {self.api_key}"})
        if response.status_code >= 400:
            raise classify_http_error(response)
        data = response.json()
        self._previous = data.get("id")
        call = next((o for o in data.get("output", []) if o.get("type") == "computer_call"), None)
        if call is None:
            return Action(action="done", reason="model finished")
        self._call_id = call.get("call_id")
        self._safety = list(call.get("pending_safety_checks") or [])
        return self.to_action(call.get("action") or {})

    @staticmethod
    def to_action(a: dict[str, Any]) -> Action:
        kind = a.get("type")
        if kind == "click":
            return Action(action="click", x=a.get("x"), y=a.get("y"))
        if kind == "double_click":
            return Action(action="double_click", x=a.get("x"), y=a.get("y"))
        if kind == "type":
            return Action(action="type", text=a.get("text", ""))
        if kind == "keypress":
            return Action(action="key", key="+".join(a.get("keys") or ["Enter"]))
        if kind == "scroll":
            return Action(action="scroll", x=a.get("x"), y=a.get("y"), dx=int(a.get("scroll_x") or 0),
                          dy=int(a.get("scroll_y") or 0))
        if kind == "wait":
            return Action(action="wait", ms=1000)
        return Action(action="wait", ms=200, reason=f"{kind}: screenshot only")


class AnthropicComputerUse(ComputerUseModel):
    name = "anthropic-computer-use"

    def __init__(self, base_url: str, api_key: str | None, model: str, client: httpx.AsyncClient) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.client = client
        self._messages: list[dict[str, Any]] = []
        self._tool_use_id: str | None = None

    async def next_action(self, goal: str, screenshot: bytes, history: list[dict[str, Any]], width: int,
                          height: int) -> Action:
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _png_b64(screenshot)}}
        if self._tool_use_id is None:
            self._messages = [{"role": "user", "content": [{"type": "text", "text": goal}, image]}]
        else:
            self._messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": self._tool_use_id, "content": [image]}]})
        body = {"model": self.model, "max_tokens": 1024, "messages": self._messages,
                "tools": [{"type": "computer_20250124", "name": "computer", "display_width_px": width,
                           "display_height_px": height}]}
        response = await self.client.post(
            f"{self.base_url}/messages", json=body, timeout=120,
            headers={"x-api-key": self.api_key or "", "anthropic-version": "2023-06-01",
                     "anthropic-beta": "computer-use-2025-01-24"})
        if response.status_code >= 400:
            raise classify_http_error(response)
        data = response.json()
        content = data.get("content", [])
        self._messages.append({"role": "assistant", "content": content})
        use = next((c for c in content if c.get("type") == "tool_use"), None)
        if use is None:
            return Action(action="done", reason="model finished")
        self._tool_use_id = use.get("id")
        return self.to_action(use.get("input") or {})

    @staticmethod
    def to_action(a: dict[str, Any]) -> Action:
        kind = a.get("action")
        x, y = (a.get("coordinate") or [None, None])[:2]
        if kind in ("left_click", "right_click", "middle_click"):
            return Action(action="click", x=x, y=y)
        if kind == "double_click":
            return Action(action="double_click", x=x, y=y)
        if kind == "type":
            return Action(action="type", text=a.get("text", ""))
        if kind == "key":
            return Action(action="key", key=str(a.get("text", "Enter")).replace("Return", "Enter"))
        if kind == "scroll":
            amount = int(a.get("scroll_amount") or 3) * 100
            direction = a.get("scroll_direction", "down")
            return Action(action="scroll", x=x, y=y, dy=amount if direction == "down" else -amount if
                          direction == "up" else 0, dx=amount if direction == "right" else -amount if
                          direction == "left" else 0)
        if kind == "wait":
            return Action(action="wait", ms=int(float(a.get("duration") or 1) * 1000))
        return Action(action="wait", ms=200, reason=f"{kind}: screenshot only")


class GeminiComputerUse(ComputerUseModel):
    name = "gemini-computer-use"

    def __init__(self, base_url: str, api_key: str | None, model: str, client: httpx.AsyncClient) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.client = client
        self._contents: list[dict[str, Any]] = []
        self._pending: str | None = None

    async def next_action(self, goal: str, screenshot: bytes, history: list[dict[str, Any]], width: int,
                          height: int) -> Action:
        image = {"inlineData": {"mimeType": "image/png", "data": _png_b64(screenshot)}}
        if self._pending is None:
            self._contents = [{"role": "user", "parts": [{"text": goal}, image]}]
        else:
            self._contents.append({"role": "user", "parts": [
                {"functionResponse": {"name": self._pending, "response": {"url": history[-1].get("url", "")
                                                                          if history else ""}}}, image]})
        body = {"contents": self._contents, "tools": [{"computerUse": {"environment": "ENVIRONMENT_BROWSER"}}]}
        model_path = self.model if self.model.startswith("models/") else f"models/{self.model}"
        response = await self.client.post(f"{self.base_url}/{model_path}:generateContent", json=body, timeout=120,
                                          headers={"x-goog-api-key": self.api_key or ""})
        if response.status_code >= 400:
            raise classify_http_error(response)
        data = response.json()
        candidate = (data.get("candidates") or [{}])[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        self._contents.append({"role": "model", "parts": parts})
        call = next((p["functionCall"] for p in parts if "functionCall" in p), None)
        if call is None:
            return Action(action="done", reason="model finished")
        self._pending = call.get("name")
        return self.to_action(call.get("name", ""), call.get("args") or {}, width, height)

    @staticmethod
    def to_action(name: str, args: dict[str, Any], width: int, height: int) -> Action:
        def px(value: Any, size: int) -> int | None:
            return None if value is None else round(float(value) / 1000 * size)

        x, y = px(args.get("x"), width), px(args.get("y"), height)
        if name in ("click_at", "hover_at"):
            return Action(action="click", x=x, y=y)
        if name == "type_text_at":
            return Action(action="type", x=x, y=y, text=str(args.get("text", "")) + (
                "\n" if args.get("press_enter") else ""))
        if name in ("scroll_document", "scroll_at"):
            direction = args.get("direction", "down")
            return Action(action="scroll", x=x, y=y, dy=600 if direction == "down" else -600 if direction == "up"
                          else 0)
        if name == "key_combination":
            return Action(action="key", key=str(args.get("keys", "Enter")))
        if name == "navigate":
            return Action(action="navigate", url=str(args.get("url", "")))
        if name.startswith("wait"):
            return Action(action="wait", ms=5000)
        return Action(action="wait", ms=200, reason=f"unsupported {name}")


async def select_model(router: ProviderRouter, meta: CallMeta, client: httpx.AsyncClient,
                       secrets_lookup: Any) -> ComputerUseModel:
    """Pick the adapter for the user's ``computer_use`` binding."""
    bindings = await router.resolve(meta.user_id, "auto", "computer_use")
    if not bindings:
        raise ProviderError("no vision-capable model is configured for the computer_use category")
    first = bindings[0]
    if first.tier == "computer_use":
        provider = await router.service.get(meta.user_id, first.provider_id)
        adapter = router.service.adapter_for(provider)
        key = adapter.api_key
        if first.kind == "openai":
            return OpenAIComputerUse(provider.base_url, key, first.model, client)
        if first.kind == "anthropic":
            return AnthropicComputerUse(provider.base_url, key, first.model, client)
        if first.kind == "google":
            return GeminiComputerUse(provider.base_url, key, first.model, client)
    return GenericVisionModel(router, meta, bindings)
