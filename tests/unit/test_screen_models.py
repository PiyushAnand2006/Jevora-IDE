from __future__ import annotations

import json

import httpx

from backend.screen_control.computer_use_agent import clamp, parse_test_request
from backend.screen_control.driver import Action, host_allowed
from backend.screen_control.models import AnthropicComputerUse, GeminiComputerUse, OpenAIComputerUse

PNG = b"\x89PNG\r\n\x1a\nfake"


def test_clamp_and_scaling() -> None:
    assert clamp(Action(action="click", x=5000, y=-20), 1280, 800).model_dump(include={"x", "y"}) == {"x": 1279, "y": 0}
    scaled = clamp(Action(action="click", x=640, y=400), 1280, 800, scale_x=0.5, scale_y=0.5)
    assert (scaled.x, scaled.y) == (320, 200)
    assert clamp(Action(action="wait"), 10, 10).x is None


def test_parse_test_request_and_allow_list() -> None:
    url, specs = parse_test_request('/test http://127.0.0.1:5000/home, expect "Hello World" and shows "Login"')
    assert url == "http://127.0.0.1:5000/home"
    assert [s.value for s in specs] == ["Hello World", "Login"]
    assert parse_test_request("run the unit tests") == (None, [])
    assert host_allowed("http://localhost:3000/x", ["localhost"])
    assert host_allowed("http://app.localhost:3000", ["localhost"])
    assert not host_allowed("https://evil.example.com", ["localhost", "127.0.0.1"])
    assert host_allowed("about:blank", [])


async def test_openai_computer_use_adapter() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            return httpx.Response(200, json={"id": "resp_1", "output": [{"type": "computer_call", "call_id": "c1",
                                                                         "action": {"type": "click", "x": 10, "y": 20},
                                                                         "pending_safety_checks": []}]})
        return httpx.Response(200, json={"id": "resp_2", "output": [{"type": "message", "content": []}]})

    model = OpenAIComputerUse("https://api.openai.test/v1", "k", "computer-use-preview",
                              httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    first = await model.next_action("click it", PNG, [], 1280, 800)
    assert first == Action(action="click", x=10, y=20)
    assert bodies[0]["tools"][0] == {"type": "computer_use_preview", "display_width": 1280, "display_height": 800,
                                     "environment": "browser"}
    done = await model.next_action("click it", PNG, [{"action": {}}], 1280, 800)
    assert done.action == "done"
    assert bodies[1]["previous_response_id"] == "resp_1"
    assert bodies[1]["input"][0]["type"] == "computer_call_output" and bodies[1]["input"][0]["call_id"] == "c1"


async def test_anthropic_computer_use_adapter() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(200, json={"content": [{"type": "tool_use", "id": "tu1", "name": "computer",
                                                          "input": {"action": "left_click", "coordinate": [30, 40]}}],
                                             "stop_reason": "tool_use"})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn"})

    model = AnthropicComputerUse("https://api.anthropic.test/v1", "k", "claude-sonnet",
                                 httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await model.next_action("go", PNG, [], 1024, 768) == Action(action="click", x=30, y=40)
    assert seen[0].headers["anthropic-beta"] == "computer-use-2025-01-24"
    tool = json.loads(seen[0].content)["tools"][0]
    assert tool["type"] == "computer_20250124" and tool["display_width_px"] == 1024
    assert (await model.next_action("go", PNG, [], 1024, 768)).action == "done"
    second = json.loads(seen[1].content)["messages"][-1]["content"][0]
    assert second["type"] == "tool_result" and second["tool_use_id"] == "tu1"
    scroll = AnthropicComputerUse.to_action({"action": "scroll", "coordinate": [1, 2], "scroll_direction": "up",
                                             "scroll_amount": 2})
    assert scroll.dy == -200


async def test_gemini_computer_use_adapter_normalised_coordinates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["tools"] == [{"computerUse": {"environment": "ENVIRONMENT_BROWSER"}}]
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "click_at", "args": {"x": 500, "y": 250}}}]}}]})

    model = GeminiComputerUse("https://gl.test/v1beta", "k", "gemini-2.5-computer-use",
                              httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    action = await model.next_action("go", PNG, [], 1280, 800)
    assert (action.action, action.x, action.y) == ("click", 640, 200)
    typed = GeminiComputerUse.to_action("type_text_at", {"x": 0, "y": 0, "text": "hi", "press_enter": True}, 100, 100)
    assert typed.text == "hi\n"
