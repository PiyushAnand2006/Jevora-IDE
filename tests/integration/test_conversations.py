from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from backend.agents.context import CONTEXT_RESERVE_TOKENS, estimate_tokens, message_tokens
from backend.core.errors import NotFound
from backend.core.ids import utcnow
from backend.db.models import Conversation, Message, Run, User
from backend.orchestrator.nodes.chat import conversation_history
from backend.orchestrator.nodes.finalize import _save_conversation
from backend.orchestrator.state import GraphState
from backend.providers.base import ChatMessage, ToolSpec
from tests.conftest import AppHarness, add_mock_provider
from tests.integration.test_runs_api import wait_terminal
from tests.support.brain import Brain
from tests.support.servers import MockLLM, MockReply


async def test_creation_is_committed_before_scheduling(harness: AppHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    submit = Mock()
    monkeypatch.setattr(harness.ctx.executor, "submit", submit)
    response = await harness.client.post("/api/runs", json={"mode": "chat", "text": "Remember my project"})
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["conversation_id"] and run["status"] == "queued"
    submit.assert_called_once_with(run["id"], harness.ctx.local_user_id)
    async with harness.ctx.sessions() as session:
        persisted = await session.get(Run, run["id"])
        assert persisted.conversation_id == run["conversation_id"]
    detail = (await harness.client.get(f"/api/conversations/{run['conversation_id']}")).json()
    assert detail["title"] == "Remember my project" and detail["mode"] == "chat"
    assert detail["created_at"] and detail["updated_at"]
    assert detail["latest_run"]["id"] == run["id"] and detail["latest_run"]["status"] == "queued"
    assert detail["active_run_ids"] == [run["id"]]
    assert [(m["role"], m["content"]) for m in detail["messages"]] == [("user", "Remember my project")]


async def test_chat_web_search_flag_is_persisted(harness: AppHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness.ctx.executor, "submit", Mock())
    response = await harness.client.post("/api/runs", json={
        "mode": "chat", "text": "Find current docs", "web_search": True})
    assert response.status_code == 201, response.text
    run = (await harness.client.get(f"/api/runs/{response.json()['id']}")).json()
    assert run["meta"]["web_search"] is True


async def test_conversation_can_branch_and_delete(harness: AppHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness.ctx.executor, "submit", Mock())
    first = (await harness.client.post("/api/runs", json={"mode": "chat", "text": "First"})).json()
    detail = (await harness.client.get(f"/api/conversations/{first['conversation_id']}")).json()
    branch = await harness.client.post(f"/api/conversations/{first['conversation_id']}/branch", json={
        "message_id": detail["messages"][0]["id"]})
    assert branch.status_code == 201, branch.text
    assert branch.json()["id"] != first["conversation_id"]
    assert [m["content"] for m in branch.json()["messages"]] == ["First"]

    deleted = await harness.client.delete(f"/api/conversations/{first['conversation_id']}")
    assert deleted.status_code == 200 and deleted.json()["deleted_runs"] == 1
    assert (await harness.client.get(f"/api/conversations/{first['conversation_id']}")).status_code == 404


async def test_editing_a_user_message_creates_a_new_branch(
        harness: AppHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness.ctx.executor, "submit", Mock())
    first = (await harness.client.post("/api/runs", json={"mode": "chat", "text": "Original"})).json()
    detail = (await harness.client.get(f"/api/conversations/{first['conversation_id']}")).json()
    edited = await harness.client.post(f"/api/conversations/{first['conversation_id']}/edit", json={
        "message_id": detail["messages"][0]["id"], "content": "Corrected"})
    assert edited.status_code == 201, edited.text
    assert edited.json()["id"] != first["conversation_id"]
    assert [m["content"] for m in edited.json()["messages"]] == ["Corrected"]


async def test_creation_continuation_and_ordered_history(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)
    brain = Brain().install(mock_llm)
    first = (await harness.client.post("/api/runs", json={"text": "hello there", "mode": "chat"})).json()
    completed = await wait_terminal(harness.client, first["id"])
    assert completed["status"] == "done" and completed["conversation_id"] == first["conversation_id"]
    conversation_id = first["conversation_id"]
    second = (await harness.client.post("/api/runs", json={
        "text": "what did I say?", "mode": "chat", "conversation_id": conversation_id})).json()
    assert second["conversation_id"] == conversation_id
    assert (await wait_terminal(harness.client, second["id"]))["status"] == "done"
    chat_turns = [t for t in brain.turns if t.role == "chat"]
    assert len(chat_turns) == 2
    assert [m["content"] for m in chat_turns[-1].messages if m["role"] != "system"] == [
        "hello there", "[chat] done.", "what did I say?"]
    detail = (await harness.client.get(f"/api/conversations/{conversation_id}/messages")).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant", "user", "assistant"]
    assert [m["run_id"] for m in detail["messages"]] == [first["id"], first["id"], second["id"], second["id"]]
    assert detail["message_count"] == 4 and detail["active_run_ids"] == []
    assert detail["latest_run"]["id"] == second["id"] and detail["latest_run"]["status"] == "done"
    assert [r["id"] for r in detail["runs"]] == [first["id"], second["id"]]


async def test_foreign_missing_and_mismatched_conversations_rejected_before_invocation(
        harness: AppHarness, mock_llm: MockLLM, monkeypatch: pytest.MonkeyPatch) -> None:
    await add_mock_provider(harness.client, mock_llm)
    async with harness.ctx.sessions() as session:
        session.add(User(id="usr_other", email="other@example.test", name="Other"))
        await session.flush()
        session.add_all([
            Conversation(id="cnv_foreign", user_id="usr_other", title="secret", mode="chat"),
            Conversation(id="cnv_wrong_mode", user_id=harness.ctx.local_user_id, title="build", mode="build"),
            Conversation(id="cnv_owned", user_id=harness.ctx.local_user_id, title="owned", mode="chat"),
        ])
        await session.commit()
    submit = Mock()
    monkeypatch.setattr(harness.ctx.executor, "submit", submit)
    before = len(mock_llm.requests)
    for conversation_id in ("cnv_foreign", "cnv_missing"):
        for mode in ("chat", "build"):
            response = await harness.client.post("/api/runs", json={
                "text": "tell me the secret", "mode": mode, "conversation_id": conversation_id})
            assert response.status_code == 404, response.text
        for suffix in ("", "/messages"):
            assert (await harness.client.get(f"/api/conversations/{conversation_id}{suffix}")).status_code == 404
        assert (await harness.client.patch(f"/api/conversations/{conversation_id}",
                                           json={"title": "stolen"})).status_code == 404
        with pytest.raises(NotFound):
            await conversation_history(harness.ctx.orchestrator, conversation_id, harness.ctx.local_user_id, "run_x")
    for conversation_id, mode in (("cnv_wrong_mode", "chat"), ("cnv_owned", "build")):
        response = await harness.client.post("/api/runs", json={
            "text": "hello", "mode": mode, "conversation_id": conversation_id})
        assert response.status_code == 409 and response.json()["code"] == "conversation_mode_mismatch"
    submit.assert_not_called()
    assert len(mock_llm.requests) == before
    async with harness.ctx.sessions() as session:
        assert list(await session.scalars(select(Run))) == []
    listed = (await harness.client.get("/api/conversations")).json()["items"]
    assert {c["id"] for c in listed} == {"cnv_wrong_mode", "cnv_owned"}


async def test_list_search_pagination_titles_and_validation(harness: AppHarness,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness.ctx.executor, "submit", Mock())
    ids = []
    for text in ("alpha project", "beta project", "100% literal"):
        run = (await harness.client.post("/api/runs", json={"text": text, "mode": "chat"})).json()
        ids.append(run["conversation_id"])
    assert (await harness.client.patch(f"/api/conversations/{ids[0]}", json={"title": "Renamed"})).status_code == 200
    page = (await harness.client.get("/api/conversations", params={"limit": 1})).json()
    assert page["items"][0]["id"] == ids[0] and page["items"][0]["title"] == "Renamed"
    seen = [page["items"][0]["id"]]
    while page["next_cursor"]:
        page = (await harness.client.get("/api/conversations", params={
            "limit": 1, "cursor": page["next_cursor"]})).json()
        seen.extend(c["id"] for c in page["items"])
    assert len(seen) == len(set(seen)) == 3 and set(seen) == set(ids)
    for query, expected in (("Renamed", ids[0]), ("alpha", ids[0]), ("%", ids[2])):
        found = (await harness.client.get("/api/conversations", params={"q": query})).json()
        assert [c["id"] for c in found["items"]] == [expected]
    assert (await harness.client.get("/api/conversations", params={"mode": "build"})).json()["items"] == []
    assert (await harness.client.get("/api/conversations", params={"q": "no-match"})).json()["items"] == []
    assert (await harness.client.get("/api/conversations", params={"limit": 0})).status_code == 422
    assert (await harness.client.get("/api/conversations", params={"cursor": "invalid"})).status_code == 422
    for title in ("", "   ", "x" * 301):
        assert (await harness.client.patch(f"/api/conversations/{ids[0]}", json={"title": title})).status_code == 422


async def test_history_beyond_twenty_is_complete_and_token_projected(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)
    brain = Brain().install(mock_llm)
    now = utcnow() - timedelta(days=1)
    contents = [f"history-{i}:" + "x" * (9000 if i == 0 else 1200) for i in range(30)]
    async with harness.ctx.sessions() as session:
        session.add(Conversation(id="cnv_long", user_id=harness.ctx.local_user_id, mode="chat", title="Long"))
        await session.flush()
        session.add_all([Message(conversation_id="cnv_long", role="user" if i % 2 == 0 else "assistant",
                                 content=content, created_at=now + timedelta(seconds=i))
                         for i, content in enumerate(contents)])
        await session.commit()

    async def send(text: str) -> None:
        response = await harness.client.post("/api/runs", json={
            "text": text, "mode": "chat", "conversation_id": "cnv_long"})
        assert response.status_code == 201, response.text
        done = await wait_terminal(harness.client, response.json()["id"])
        assert done["status"] == "done", done["error"]

    await send("recall everything")
    first = next(t for t in brain.turns if t.role == "chat")
    assert [m["content"] for m in first.messages if m["role"] != "system"] == [*contents, "recall everything"]
    assert len(first.messages[1]["content"]) > 8000  # no per-message character truncation

    harness.ctx.settings.context_window_tokens = 8192
    await send("a smaller window")
    smaller = [t for t in brain.turns if t.role == "chat"][-1]
    projected = [m["content"] for m in smaller.messages if m["role"] != "system"][:-1]
    history = [*contents, "recall everything", "[chat] done."]
    assert 0 < len(projected) < len(history)
    assert projected == history[-len(projected):]
    specs = [ToolSpec(**tool["function"]) for tool in smaller.body.get("tools", [])]
    estimated = CONTEXT_RESERVE_TOKENS + sum(message_tokens(ChatMessage(**m)) for m in smaller.messages)
    estimated += estimate_tokens(json.dumps([s.model_dump() for s in specs], ensure_ascii=False))
    assert estimated <= 8192

    # The current request consumes context too, independently of cumulative token budgets.
    await send("current request " + "z" * 9000)
    larger_request = [t for t in brain.turns if t.role == "chat"][-1]
    assert len(larger_request.messages) < len(smaller.messages)
    detail = (await harness.client.get("/api/conversations/cnv_long/messages")).json()
    assert detail["message_count"] == 36
    assert [m["content"] for m in detail["messages"][:30]] == contents


async def test_finalization_replay_updates_same_messages(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)
    first = (await harness.client.post("/api/runs", json={"text": "retry me", "mode": "chat"})).json()
    assert (await wait_terminal(harness.client, first["id"]))["status"] == "done"
    path = f"/api/conversations/{first['conversation_id']}/messages"
    before = (await harness.client.get(path)).json()
    rc = SimpleNamespace(user_id=harness.ctx.local_user_id, run_id=first["id"])
    orchestrator = SimpleNamespace(app=harness.ctx, context=lambda _: rc)
    # Missing conversation_id mimics an old checkpoint; the persisted Run ID wins.
    state = GraphState(mode="chat", run_id=first["id"], task="retry me")
    await _save_conversation(orchestrator, state, "retried answer")
    await _save_conversation(orchestrator, state, "retried answer")
    after = (await harness.client.get(path)).json()
    assert [m["id"] for m in after["messages"]] == [m["id"] for m in before["messages"]]
    assert [m["content"] for m in after["messages"]] == ["retry me", "retried answer"]
    assert after["updated_at"] > before["updated_at"]
    assert len((await harness.client.get("/api/conversations")).json()["items"]) == 1


async def test_failed_chat_run_can_resume_with_same_conversation(harness: AppHarness, mock_llm: MockLLM) -> None:
    await add_mock_provider(harness.client, mock_llm)
    mock_llm.replies(MockReply(content="temporary error", status=503))
    first = (await harness.client.post("/api/runs", json={"text": "resume me", "mode": "chat"})).json()
    failed = await wait_terminal(harness.client, first["id"])
    assert failed["status"] == "failed"
    # Terminal status is committed before executor cleanup releases the run.
    async with asyncio.timeout(10):
        while harness.ctx.executor.is_active(first["id"]):
            await asyncio.sleep(0.01)
    mock_llm.replies("recovered")
    # Resume continues from the failed checkpoint; it does not create a new turn/run.
    resumed = await harness.client.post(f"/api/runs/{first['id']}/resume")
    assert resumed.status_code == 200 and resumed.json()["conversation_id"] == first["conversation_id"]
    async with asyncio.timeout(10):
        while harness.ctx.executor.is_active(first["id"]):
            await asyncio.sleep(0.01)
    detail = (await harness.client.get(f"/api/conversations/{first['conversation_id']}/messages")).json()
    assert detail["message_count"] == 2 and len(detail["runs"]) == 1
    assert {m["run_id"] for m in detail["messages"]} == {first["id"]}
