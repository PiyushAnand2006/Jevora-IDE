from __future__ import annotations

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from backend.db.models import LLMCall
from backend.providers.base import ChatMessage, ContentPart, NoProviderConfigured, ProviderError, ToolSpec
from backend.providers.router import CallMeta
from tests.conftest import AppHarness, add_mock_provider
from tests.support.servers import MockLLM, MockReply


def _meta(harness: AppHarness) -> CallMeta:
    return CallMeta(user_id=harness.ctx.local_user_id, run_id=None)


async def test_routes_two_distinct_tiers(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)
    router = harness.ctx.router
    fast = await router.chat(_meta(harness), [ChatMessage(role="user", content="classify")],
                             category="fast_classification")
    heavy = await router.chat(_meta(harness), [ChatMessage(role="user", content="design a system")],
                              category="heavy_reasoning")
    assert fast.model == "mock-mini"
    assert heavy.model == "mock-pro"
    tiers = {(await router.resolve(harness.ctx.local_user_id, "auto", c))[0].tier
             for c in ("fast_classification", "heavy_reasoning")}
    assert tiers == {"fast", "reasoning"}


async def test_explicit_model_overrides_auto(harness: AppHarness, mock_llm: MockLLM) -> None:
    provider = await add_mock_provider(harness.client, mock_llm)
    response = await harness.ctx.router.chat(
        _meta(harness), [ChatMessage(role="user", content="hi")], model=f"{provider['id']}:mock-mini",
        category="heavy_reasoning")
    assert response.model == "mock-mini"
    by_name = await harness.ctx.router.chat(_meta(harness), [ChatMessage(role="user", content="hi")],
                                            model="mock-pro", category="fast_classification")
    assert by_name.model == "mock-pro"


async def test_local_alias_resolves_to_local_provider(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)  # localhost URL → kind "local"
    chain = await harness.ctx.router.resolve(harness.ctx.local_user_id, "local", "code_generation")
    assert chain[0].kind == "local" and chain[0].model in {"mock-mini", "mock-pro"}


async def test_no_provider_configured(harness: AppHarness) -> None:
    with pytest.raises(NoProviderConfigured):
        await harness.ctx.router.chat(_meta(harness), [ChatMessage(role="user", content="hi")])


async def test_retries_429_then_succeeds_and_records_usage(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)
    mock_llm.replies(MockReply(status=429, headers={"retry-after": "0"}), MockReply(content="after retry"))
    response = await harness.ctx.router.chat(_meta(harness), [ChatMessage(role="user", content="hi")],
                                             category="fast_classification")
    assert response.content == "after retry"
    assert response.attempts == 2
    async with harness.ctx.sessions() as session:
        rows = list(await session.scalars(select(LLMCall)))
    assert rows and rows[-1].status == "ok" and rows[-1].prompt_tokens == 11


async def test_fallback_to_next_binding_on_failure(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)

    def handler(body: dict) -> MockReply:
        return MockReply(status=400) if body["model"] == "mock-pro" else MockReply(content="from fallback")

    mock_llm.handler = handler
    response = await harness.ctx.router.chat(_meta(harness), [ChatMessage(role="user", content="hi")],
                                             category="heavy_reasoning")
    assert response.content == "from fallback" and response.fallback_used and response.model == "mock-mini"


async def test_provider_down_mid_stream_falls_back(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)

    def handler(body: dict) -> MockReply:
        if body["model"] == "mock-pro":
            return MockReply(content="partial words that will be cut off", drop_after_chunks=2)
        return MockReply(content="complete answer")

    mock_llm.handler = handler
    deltas: list[str] = []
    resets = 0

    async def on_delta(text: str) -> None:
        deltas.append(text)

    async def on_reset() -> None:
        nonlocal resets
        resets += 1
        deltas.clear()

    response = await harness.ctx.router.chat(
        _meta(harness), [ChatMessage(role="user", content="hi")], category="heavy_reasoning",
        stream=True, on_delta=on_delta, on_reset=on_reset)
    assert response.content == "complete answer"
    assert resets == 1 and "".join(deltas) == "complete answer"


async def test_all_bindings_fail_raises(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)
    mock_llm.replies(MockReply(status=500))
    with pytest.raises(ProviderError):
        await harness.ctx.router.chat(_meta(harness), [ChatMessage(role="user", content="hi")])


async def test_circuit_breaker_opens(harness: AppHarness, mock_llm: MockLLM) -> None:
    provider = await add_mock_provider(harness.client, mock_llm)
    mock_llm.replies(MockReply(status=503))
    for _ in range(2):
        with pytest.raises(ProviderError):
            await harness.ctx.router.chat(_meta(harness), [ChatMessage(role="user", content="hi")])
    assert harness.ctx.router.breakers.get(provider["id"]).state == "open"


async def test_tool_calls_normalised(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)
    mock_llm.replies(MockReply(tool_calls=[{"id": "t1", "name": "read_file", "arguments": {"path": "a.py"}}]))
    response = await harness.ctx.router.chat(
        _meta(harness), [ChatMessage(role="user", content="read a.py")],
        tools=[ToolSpec(name="read_file", parameters={"type": "object", "properties": {"path": {"type": "string"}}})])
    assert response.tool_calls[0].name == "read_file"
    assert response.tool_calls[0].arguments == {"path": "a.py"}
    sent = mock_llm.requests[-1]
    assert sent["tools"][0]["function"]["name"] == "read_file"


class Verdict(BaseModel):
    choice: str
    score: float


async def test_malformed_json_is_repaired_or_reprompted(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)
    mock_llm.replies("```json\n{'choice': 'yes', 'score': 0.5,}\n```")
    parsed, _ = await harness.ctx.router.chat_json(_meta(harness), [ChatMessage(role="user", content="q")], Verdict)
    assert parsed == Verdict(choice="yes", score=0.5)

    mock_llm.replies("not json at all", '{"choice": "no", "score": 1}')
    parsed, _ = await harness.ctx.router.chat_json(_meta(harness), [ChatMessage(role="user", content="q")], Verdict)
    assert parsed.choice == "no"
    assert "not valid JSON" in str(mock_llm.requests[-1]["messages"][-1]["content"])


async def test_images_stripped_for_non_vision_models(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)
    message = ChatMessage(role="user", content=[ContentPart(type="text", text="what is this?"),
                                                ContentPart(type="image", image_b64="aGVsbG8=", mime="image/png")])
    await harness.ctx.router.chat(_meta(harness), [message])
    sent = mock_llm.requests[-1]["messages"][0]["content"]
    assert all(part["type"] == "text" for part in sent)
    assert "omitted" in sent[-1]["text"]


async def test_embeddings(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)
    vectors = await harness.ctx.router.embed(harness.ctx.local_user_id, ["hello world", "bye"])
    assert vectors is not None and len(vectors) == 2 and len(vectors[0]) == 16
