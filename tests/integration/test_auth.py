from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from backend.core.security import REFRESH_COOKIE
from tests.conftest import AppHarness, make_settings, start_app


@pytest.fixture
async def secured(tmp_path: Path) -> AsyncIterator[AppHarness]:
    settings = make_settings(tmp_path, auth_required=True, login_rate_limit_per_minute=3,
                             google_client_id="gid", google_client_secret="gsecret")
    app, lifespan = await start_app(settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        yield AppHarness(app, client)
    await lifespan.__aexit__(None, None, None)


async def _signup(client: httpx.AsyncClient, email: str = "ada@example.com") -> dict:
    response = await client.post("/api/auth/signup", json={"name": "Ada", "email": email,
                                                           "password": "correct-horse-1"})
    assert response.status_code == 201, response.text
    return response.json()


async def test_local_mode_works_without_login(client: httpx.AsyncClient) -> None:
    me = await client.get("/api/auth/me")
    assert me.status_code == 200 and me.json()["is_local"] is True
    assert (await client.get("/api/providers")).status_code == 200


async def test_invalid_token_rejected_even_in_local_mode(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/providers", headers={"Authorization": "Bearer garbage"})
    assert response.status_code == 401 and response.json()["code"] == "invalid_token"


async def test_auth_required_blocks_api(secured: AppHarness) -> None:
    client = secured.client
    response = await client.get("/api/providers")
    assert response.status_code == 401 and response.json()["code"] == "auth_required"
    assert (await client.get("/api/auth/config")).json()["auth_required"] is True
    assert (await client.get("/api/features")).status_code == 200  # public


async def test_signup_login_me_refresh_logout(secured: AppHarness) -> None:
    client = secured.client
    created = await _signup(client)
    assert created["user"]["email"] == "ada@example.com" and "password" not in json.dumps(created)
    assert REFRESH_COOKIE in client.cookies

    headers = {"Authorization": f"Bearer {created['access_token']}"}
    me = await client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200 and me.json()["name"] == "Ada"
    assert (await client.get("/api/providers", headers=headers)).status_code == 200

    duplicate = await client.post("/api/auth/signup", json={"email": "ADA@example.com", "password": "another-pass"})
    assert duplicate.status_code == 409

    login = await client.post("/api/auth/login", json={"email": "ada@example.com", "password": "correct-horse-1"})
    assert login.status_code == 200 and login.json()["access_token"]

    old_refresh = client.cookies[REFRESH_COOKIE]
    refreshed = await client.post("/api/auth/refresh")
    assert refreshed.status_code == 200
    new_refresh = client.cookies[REFRESH_COOKIE]
    assert new_refresh != old_refresh

    # Reusing a rotated refresh token revokes the whole family.
    client.cookies.set(REFRESH_COOKIE, old_refresh, path="/api/auth")
    reuse = await client.post("/api/auth/refresh")
    assert reuse.status_code == 401 and reuse.json()["code"] == "refresh_reuse"
    client.cookies.set(REFRESH_COOKIE, new_refresh, path="/api/auth")
    assert (await client.post("/api/auth/refresh")).status_code == 401

    login = await client.post("/api/auth/login", json={"email": "ada@example.com", "password": "correct-horse-1"})
    assert (await client.post("/api/auth/logout")).json() == {"ok": True}
    assert (await client.post("/api/auth/refresh")).status_code == 401


async def test_wrong_password_and_rate_limit(secured: AppHarness) -> None:
    client = secured.client
    await _signup(client, "bob@example.com")
    for _ in range(3):
        bad = await client.post("/api/auth/login", json={"email": "bob@example.com", "password": "nope"})
        assert bad.status_code == 401 and bad.json()["code"] == "invalid_credentials"
    limited = await client.post("/api/auth/login", json={"email": "bob@example.com", "password": "nope"})
    assert limited.status_code == 429 and "retry-after" in limited.headers


async def test_expired_access_token(secured: AppHarness) -> None:
    created = await _signup(secured.client, "eve@example.com")
    expired = secured.ctx.tokens.issue_access(created["user"]["id"], ttl=timedelta(seconds=-1))
    response = await secured.client.get("/api/auth/me", headers={"Authorization": f"Bearer {expired.token}"})
    assert response.status_code == 401 and response.json()["code"] == "token_expired"


async def test_signup_validation(secured: AppHarness) -> None:
    assert (await secured.client.post("/api/auth/signup", json={"email": "bad", "password": "longenough"})
            ).status_code == 422
    assert (await secured.client.post("/api/auth/signup", json={"email": "a@b.co", "password": "short"})
            ).status_code == 422


async def test_oauth_not_configured_returns_501(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/auth/oauth/github/start", params={"format": "json"})
    assert response.status_code == 501
    assert response.json()["code"] == "oauth_not_configured" and "GITHUB_CLIENT_ID" in response.json()["detail"]
    assert (await client.get("/api/auth/oauth/myspace/start")).status_code == 404


async def test_google_oauth_flow(secured: AppHarness) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            form = parse_qs(request.content.decode())
            assert form["code"] == ["the-code"] and form["code_verifier"]
            return httpx.Response(200, json={"access_token": "google-access"})
        if request.url.path == "/v1/userinfo":
            assert request.headers["authorization"] == "Bearer google-access"
            return httpx.Response(200, json={"sub": "g-123", "email": "grace@example.com",
                                             "email_verified": True, "name": "Grace"})
        return httpx.Response(404)

    secured.ctx.oauth_transport = httpx.MockTransport(handler)
    secured.ctx.settings.google_token_url = "https://oauth.test/token"
    secured.ctx.settings.google_userinfo_url = "https://oauth.test/v1/userinfo"
    client = secured.client
    start = await client.get("/api/auth/oauth/google/start", params={"format": "json", "next": "/index.html"})
    assert start.status_code == 200
    state = parse_qs(urlparse(start.json()["url"]).query)["state"][0]

    bad = await client.get("/api/auth/oauth/google/callback", params={"code": "the-code", "state": "forged"})
    assert bad.status_code == 302 and "invalid_state" in bad.headers["location"]

    start = await client.get("/api/auth/oauth/google/start", params={"format": "json"})
    state = parse_qs(urlparse(start.json()["url"]).query)["state"][0]
    callback = await client.get("/api/auth/oauth/google/callback", params={"code": "the-code", "state": state})
    assert callback.status_code == 302 and callback.headers["location"].endswith("auth=success")
    refreshed = await client.post("/api/auth/refresh")
    assert refreshed.status_code == 200 and refreshed.json()["user"]["email"] == "grace@example.com"
