from __future__ import annotations

import json
import re
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
    unknown = await client.post("/api/auth/login", json={"email": "missing@example.com", "password": "nope"})
    assert unknown.status_code == 404 and unknown.json()["code"] == "email_not_registered"
    for _ in range(3):
        bad = await client.post("/api/auth/login", json={"email": "bob@example.com", "password": "nope"})
        assert bad.status_code == 401 and bad.json()["code"] == "invalid_credentials"
    limited = await client.post("/api/auth/login", json={"email": "bob@example.com", "password": "nope"})
    assert limited.status_code == 429 and "retry-after" in limited.headers


async def test_password_reset_rejects_unregistered_email_without_sending(secured: AppHarness) -> None:
    response = await secured.client.post("/api/auth/forgot-password", json={"email": "missing@example.com"})
    assert response.status_code == 404
    assert response.json() == {"detail": "email not registered", "code": "email_not_registered"}
    assert secured.ctx.mailer.outbox == []


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


async def test_password_reset_otp_updates_hashed_password_and_revokes_sessions(secured: AppHarness) -> None:
    client = secured.client
    created = await _signup(client, "reset@example.com")
    old_access = created["access_token"]

    requested = await client.post("/api/auth/forgot-password", json={"email": "RESET@example.com"})
    assert requested.status_code == 200
    assert requested.json() == {"ok": True, "message": "verification code sent"}
    message = secured.ctx.mailer.outbox[-1].body
    assert secured.ctx.mailer.outbox[-1].recipient == "reset@example.com"
    match = re.search(r"code is: (\d{6})", message)
    assert match is not None
    otp = match.group(1)

    wrong = await client.post("/api/auth/verify-password-reset-otp", json={
        "email": "reset@example.com", "otp": "000000" if otp != "000000" else "111111"})
    assert wrong.status_code == 401
    verified = await client.post("/api/auth/verify-password-reset-otp", json={
        "email": "reset@example.com", "otp": otp})
    assert verified.status_code == 200 and verified.json()["reset_token"]

    reset = await client.post("/api/auth/reset-password", json={
        "reset_token": verified.json()["reset_token"], "new_password": "new-correct-horse-2"})
    assert reset.status_code == 200
    me_old = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {old_access}"})
    assert me_old.status_code == 200  # Access JWTs remain valid until expiry.

    old_login = await client.post("/api/auth/login", json={"email": "reset@example.com", "password": "correct-horse-1"})
    assert old_login.status_code == 401
    new_login = await client.post("/api/auth/login", json={
        "email": "reset@example.com", "password": "new-correct-horse-2"})
    assert new_login.status_code == 200

    async with secured.ctx.sessions() as session:
        from backend.db.models import User
        user = await session.get(User, created["user"]["id"])
    assert user is not None and user.password_hash and "new-correct-horse-2" not in user.password_hash


async def test_password_reset_sends_the_verifiable_otp_through_smtp(secured: AppHarness, monkeypatch) -> None:
    sent = []

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: int) -> None:
            assert (host, port, timeout) == ("smtp.example.com", 587, 20)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def starttls(self) -> None:
            pass

        def send_message(self, message) -> None:
            sent.append(message)

    monkeypatch.setattr("backend.core.email.smtplib.SMTP", FakeSMTP)
    secured.ctx.settings.smtp_host = "smtp.example.com"
    await _signup(secured.client, "smtp@example.com")

    response = await secured.client.post("/api/auth/forgot-password", json={"email": "smtp@example.com"})
    assert response.status_code == 200
    assert len(sent) == 1
    assert sent[0]["To"] == "smtp@example.com"
    assert sent[0]["Subject"] == "Your Jevora IDE password reset code"
    match = re.search(r"code is: (\d{6})", sent[0].get_content())
    assert match is not None

    verified = await secured.client.post("/api/auth/verify-password-reset-otp", json={
        "email": "smtp@example.com", "otp": match.group(1)})
    assert verified.status_code == 200


async def test_authenticated_history_and_settings_are_user_scoped(secured: AppHarness) -> None:
    client = secured.client
    first = await _signup(client, "first@example.com")
    first_headers = {"Authorization": f"Bearer {first['access_token']}"}
    created_run = await client.post("/api/runs", headers=first_headers,
                                    json={"text": "first user's private history", "mode": "chat"})
    assert created_run.status_code == 201
    await client.post("/api/providers", headers=first_headers,
                      json={"name": "First provider", "base_url": "http://localhost:9999/v1"})
    await client.put("/api/decision/config", headers=first_headers,
                     json={"escalation_threshold": 0.91})

    second = await _signup(client, "second@example.com")
    second_headers = {"Authorization": f"Bearer {second['access_token']}"}
    assert (await client.get("/api/conversations", headers=second_headers)).json()["items"] == []
    assert (await client.get("/api/providers", headers=second_headers)).json() == []
    second_config = (await client.get("/api/decision/config", headers=second_headers)).json()
    assert second_config["config"]["escalation_threshold"] == 0.65

    first_history = (await client.get("/api/conversations", headers=first_headers)).json()["items"]
    assert any("first user's private history" in item["title"] for item in first_history)
    assert (await client.get("/api/providers", headers=first_headers)).json()[0]["name"] == "First provider"
    first_config = (await client.get("/api/decision/config", headers=first_headers)).json()
    assert first_config["config"]["escalation_threshold"] == 0.91


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
