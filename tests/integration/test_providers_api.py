from __future__ import annotations

import httpx
from sqlalchemy import select

from backend.db.models import ModelBinding, Provider
from tests.conftest import AppHarness, add_mock_provider
from tests.support.servers import MockLLM

SECRET = "sk-test-SUPERSECRET-0123456789"


async def test_provider_crud_and_keys_never_returned(harness: AppHarness, mock_llm: MockLLM) -> None:
    client = harness.client
    created = await client.post("/api/providers", json={
        "name": "My Mock", "base_url": mock_llm.base_url + "/", "api_key": SECRET, "kind": "custom"})
    assert created.status_code == 201
    body = created.json()
    assert set(body) >= {"id", "name", "base_url", "kind", "has_key", "created_at"}
    assert body["has_key"] is True
    assert body["base_url"] == mock_llm.base_url  # trailing slash normalised
    assert body["kind"] == "local"  # auto-detected from a localhost URL
    assert SECRET not in created.text

    listing = await client.get("/api/providers")
    assert listing.status_code == 200 and SECRET not in listing.text
    assert [p["name"] for p in listing.json()] == ["My Mock"]

    # Stored encrypted at rest.
    async with harness.ctx.sessions() as session:
        row = await session.scalar(select(Provider).where(Provider.id == body["id"]))
    assert row is not None and row.encrypted_key and SECRET not in row.encrypted_key
    assert harness.ctx.secrets.decrypt(row.encrypted_key) == SECRET

    # Empty api_key on update keeps the existing key.
    updated = await client.put(f"/api/providers/{body['id']}", json={
        "name": "Renamed", "base_url": mock_llm.base_url, "api_key": "", "kind": "custom"})
    assert updated.status_code == 200
    assert updated.json()["name"] == "Renamed" and updated.json()["has_key"] is True
    async with harness.ctx.sessions() as session:
        row = await session.get(Provider, body["id"])
    assert row is not None and harness.ctx.secrets.decrypt(row.encrypted_key or "") == SECRET

    deleted = await client.delete(f"/api/providers/{body['id']}")
    assert deleted.status_code == 200
    assert (await client.get("/api/providers")).json() == []
    assert (await client.delete(f"/api/providers/{body['id']}")).status_code == 404


async def test_duplicate_provider_name_conflicts(client: httpx.AsyncClient, mock_llm: MockLLM) -> None:
    first = await client.post("/api/providers", json={"name": "Dup", "base_url": mock_llm.base_url, "api_key": ""})
    assert first.status_code == 201
    second = await client.post("/api/providers", json={"name": "Dup", "base_url": mock_llm.base_url})
    assert second.status_code == 409
    assert second.json()["existing_id"] == first.json()["id"]


async def test_invalid_provider_payloads(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/providers", json={"name": "x", "base_url": "not a url"})).status_code == 422
    assert (await client.post("/api/providers", json={"name": "", "base_url": "http://a.b"})).status_code == 422


async def test_predefined_provider_kinds(client: httpx.AsyncClient) -> None:
    for name, url, kind in [("OpenAI", "https://api.openai.com/v1", "openai"),
                            ("Google Gemini", "https://generativelanguage.googleapis.com/v1beta", "google"),
                            ("Anthropic Claude", "https://api.anthropic.com/v1", "anthropic")]:
        response = await client.post("/api/providers", json={"name": name, "base_url": url, "api_key": "k",
                                                             "kind": "custom"})
        assert response.status_code == 201 and response.json()["kind"] == kind


async def test_models_endpoint_success(client: httpx.AsyncClient, mock_llm: MockLLM) -> None:
    mock_llm.required_key = "sk-test-mock-key-123456"
    provider = await add_mock_provider(client, mock_llm)
    response = await client.get(f"/api/providers/{provider['id']}/models")
    assert response.status_code == 200
    assert response.json() == {"models": ["mock-embed", "mock-mini", "mock-pro"]}


async def test_models_endpoint_failures_return_200(client: httpx.AsyncClient, mock_llm: MockLLM) -> None:
    provider = await add_mock_provider(client, mock_llm)
    pid = provider["id"]

    mock_llm.models_status = 500
    body = (await client.get(f"/api/providers/{pid}/models")).json()
    assert body["models"] == [] and "500" in body["error"]

    mock_llm.models_status = 200
    mock_llm.required_key = "something-else"
    body = (await client.get(f"/api/providers/{pid}/models")).json()
    assert body["models"] == [] and "401" in body["error"]

    mock_llm.required_key = None
    mock_llm.models = []
    response = await client.get(f"/api/providers/{pid}/models")
    assert response.status_code == 200 and response.json() == {"models": []}

    mock_llm.models_delay = 3.0  # > provider_models_timeout_s (2s)
    body = (await client.get(f"/api/providers/{pid}/models")).json()
    assert body["models"] == [] and "timed out" in body["error"]


async def test_models_endpoint_unreachable_host(client: httpx.AsyncClient) -> None:
    created = await client.post("/api/providers", json={"name": "Down", "base_url": "http://127.0.0.1:9/v1"})
    body = (await client.get(f"/api/providers/{created.json()['id']}/models")).json()
    assert body["models"] == [] and body["error"]


async def test_model_catalogue_and_router_config(client: httpx.AsyncClient, mock_llm: MockLLM) -> None:
    provider = await add_mock_provider(client, mock_llm)
    catalogue = (await client.get("/api/models")).json()
    ids = {m["id"] for m in catalogue["models"]}
    assert f"{provider['id']}:mock-mini" in ids
    tiers = {m["model"]: m["tier"] for m in catalogue["models"]}
    assert tiers["mock-mini"] == "fast" and tiers["mock-pro"] == "reasoning"
    assert catalogue["auto"]["categories"]["fast_classification"]["model"] == "mock-mini"
    assert catalogue["auto"]["categories"]["heavy_reasoning"]["model"] == "mock-pro"
    assert catalogue["aliases"]["gpt-4o"]["available"] is False

    config = (await client.get("/api/router/config")).json()
    assert config["tiers"]["fast_classification"] == "fast"
    assert config["tiers"]["heavy_reasoning"] == "reasoning"

    put = await client.put("/api/router/config", json={"mapping": {
        "fast_classification": [{"provider_id": provider["id"], "model": "mock-pro"}]}})
    assert put.status_code == 200
    assert put.json()["effective"]["fast_classification"][0]["model"] == "mock-pro"
    assert put.json()["effective"]["fast_classification"][0]["source"] == "router_config"

    bad = await client.put("/api/router/config", json={"mapping": {"nope": []}})
    assert bad.status_code == 422

    # Deleting the provider removes it from router config (falls back to Auto).
    await client.delete(f"/api/providers/{provider['id']}")
    config = (await client.get("/api/router/config")).json()
    assert config["overrides"] == {}


async def test_web_search_status_uses_saved_provider_capabilities(harness: AppHarness,
                                                                  mock_llm: MockLLM) -> None:
    custom = await add_mock_provider(harness.client, mock_llm)
    unavailable = (await harness.client.get("/api/web-search/status",
                                            params={"model": f"{custom['id']}:mock-pro"})).json()
    assert unavailable["available"] is False
    assert "does not support native web search" in unavailable["reason"]

    openai = (await harness.client.post("/api/providers", json={
        "name": "OpenAI", "base_url": "https://api.openai.com/v1", "api_key": "sk-test",
        "kind": "custom"})).json()
    async with harness.ctx.sessions() as session:
        session.add(ModelBinding(provider_id=openai["id"], model="gpt-4o", tier="balanced"))
        await session.commit()
    available = (await harness.client.get("/api/web-search/status",
                                          params={"model": f"{openai['id']}:gpt-4o"})).json()
    assert available["available"] is True and available["mode"] == "native"
    assert available["binding"]["provider_id"] == openai["id"]
    automatic = (await harness.client.get("/api/web-search/status")).json()
    assert automatic["available"] is True and automatic["mode"] == "native"
    assert automatic["binding"]["provider_id"] == openai["id"]
