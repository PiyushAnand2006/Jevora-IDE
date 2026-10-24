from __future__ import annotations

import httpx

from backend.providers.anthropic import AnthropicAdapter
from backend.providers.base import ChatMessage, ChatRequest
from backend.providers.gemini import GeminiAdapter
from backend.providers.openai_compat import OpenAICompatAdapter


async def test_openai_native_web_search_uses_responses_and_returns_sources() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.read()
        return httpx.Response(200, json={
            "model": "gpt-4o",
            "output_text": "The answer is 42.",
            "output": [{"type": "message", "content": [{
                "type": "output_text", "text": "The answer is 42.",
                "annotations": [{"type": "url_citation", "url": "https://example.test/answer",
                                 "title": "Answer source"}],
            }]}],
            "usage": {"input_tokens": 4, "output_tokens": 5},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAICompatAdapter("https://api.openai.com/v1", "sk-test", client)
        response = await adapter.chat(ChatRequest(model="gpt-4o", messages=[
            ChatMessage(role="user", content="What is the answer?")], web_search=True))

    assert seen["url"] == "https://api.openai.com/v1/responses"
    assert b'"web_search_preview"' in seen["body"]  # type: ignore[operator]
    assert response.content == "The answer is 42."
    assert response.sources == [{"kind": "web", "title": "Answer source",
                                 "location": "https://example.test/answer", "excerpt": ""}]


async def test_gemini_native_web_search_parses_grounding_sources() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models/gemini-2.5-pro:generateContent")
        body = request.read()
        assert b'"googleSearch"' in body
        return httpx.Response(200, json={
            "candidates": [{
                "content": {"parts": [{"text": "Grounded answer"}]},
                "finishReason": "STOP",
                "groundingMetadata": {"groundingChunks": [{"web": {
                    "uri": "https://example.test/gemini", "title": "Gemini source"}}]},
            }],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 4},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = GeminiAdapter("https://generativelanguage.googleapis.com/v1beta", "key", client)
        response = await adapter.chat(ChatRequest(model="gemini-2.5-pro", messages=[
            ChatMessage(role="user", content="Search this")], web_search=True))

    assert response.content == "Grounded answer"
    assert response.sources[0]["location"] == "https://example.test/gemini"


async def test_anthropic_native_web_search_sends_server_tool_and_parses_citations() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        assert b'"type":"web_search_20250305"' in body
        return httpx.Response(200, json={
            "model": "claude-sonnet-4",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "Anthropic answer", "citations": [{
                "url": "https://example.test/anthropic", "title": "Anthropic source",
                "cited_text": "A cited passage",
            }]}],
            "usage": {"input_tokens": 3, "output_tokens": 4},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = AnthropicAdapter("https://api.anthropic.com/v1", "key", client)
        response = await adapter.chat(ChatRequest(model="claude-sonnet-4", messages=[
            ChatMessage(role="user", content="Search this")], web_search=True))

    assert response.content == "Anthropic answer"
    assert response.sources[0]["excerpt"] == "A cited passage"
