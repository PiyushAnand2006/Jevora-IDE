"""Authentication: email/password, JWT access + rotating refresh cookie, OAuth2 (Google, GitHub)."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update

from backend.api.deps import Ctx, CurrentUser
from backend.app_context import AppContext
from backend.core.errors import AppError, Conflict, NotConfigured, NotFound, RateLimited, Unauthorized
from backend.core.ids import new_id, utcnow
from backend.core.logging import get_logger
from backend.core.security import (
    OAUTH_STATE_COOKIE,
    REFRESH_COOKIE,
    TokenError,
    hash_otp,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    verify_otp,
    verify_password,
)
from backend.core.users import user_public
from backend.db.models import OAuthAccount, PasswordResetChallenge, RefreshToken, User

log = get_logger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
OAUTH_PROVIDERS = ("google", "github")


def _normalize_email(value: str) -> str:
    email = value.strip().lower()
    if not _EMAIL_RE.match(email) or len(email) > 320:
        raise ValueError("invalid email address")
    return email


class SignupBody(BaseModel):
    name: str = Field(default="", max_length=200)
    email: str
    password: str = Field(min_length=8, max_length=256)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return _normalize_email(value)


class LoginBody(BaseModel):
    email: str
    password: str = Field(min_length=1, max_length=256)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return _normalize_email(value)


class ForgotPasswordBody(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return _normalize_email(value)


class VerifyOtpBody(BaseModel):
    email: str
    otp: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return _normalize_email(value)


class ResetPasswordBody(BaseModel):
    reset_token: str = Field(min_length=20, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _set_refresh_cookie(ctx: AppContext, response: Response, raw: str) -> None:
    response.set_cookie(
        REFRESH_COOKIE, raw, httponly=True, samesite="lax", path="/api/auth",
        secure=ctx.settings.public_base_url.startswith("https"),
        max_age=int(ctx.tokens.refresh_ttl.total_seconds()),
    )


async def _issue_session(ctx: AppContext, user: User, response: Response, request: Request,
                         family_id: str | None = None) -> dict[str, Any]:
    raw, digest = new_refresh_token()
    async with ctx.sessions() as session:
        session.add(RefreshToken(
            user_id=user.id, token_hash=digest, family_id=family_id or new_id("fam_"),
            expires_at=utcnow() + ctx.tokens.refresh_ttl,
            user_agent=(request.headers.get("user-agent") or "")[:300]))
        db_user = await session.get(User, user.id)
        if db_user is not None:
            db_user.last_login_at = utcnow()
        await session.commit()
    _set_refresh_cookie(ctx, response, raw)
    access = ctx.tokens.issue_access(user.id)
    return {"access_token": access.token, "token_type": "bearer", "expires_in": access.expires_in,
            "user": user_public(user)}


@router.get("/config")
async def auth_config(ctx: Ctx) -> dict[str, Any]:
    return {"auth_required": ctx.settings.auth_required, "oauth": ctx.settings.oauth_configured}


@router.post("/signup", status_code=201)
async def signup(body: SignupBody, request: Request, response: Response, ctx: Ctx) -> dict[str, Any]:
    allowed, retry = ctx.ip_limiter.hit(f"signup:{_client_ip(request)}")
    if not allowed:
        raise RateLimited("too many sign-up attempts; try again shortly", retry_after=retry)
    async with ctx.sessions() as session:
        if await session.scalar(select(User).where(User.email == body.email)) is not None:
            raise Conflict("an account with this email already exists", code="email_taken")
        user = User(email=body.email, name=body.name.strip() or body.email.split("@")[0],
                    password_hash=hash_password(body.password))
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return await _issue_session(ctx, user, response, request)


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response, ctx: Ctx) -> dict[str, Any]:
    ip = _client_ip(request)
    for limiter, key in ((ctx.ip_limiter, f"login-ip:{ip}"), (ctx.login_limiter, f"login:{ip}:{body.email}")):
        allowed, retry = limiter.hit(key)
        if not allowed:
            raise RateLimited("too many login attempts; try again shortly", retry_after=retry)
    async with ctx.sessions() as session:
        user = await session.scalar(select(User).where(User.email == body.email))
    if user is None:
        raise NotFound("email not registered", code="email_not_registered")
    if not verify_password(user.password_hash, body.password):
        raise Unauthorized("invalid email or password", code="invalid_credentials")
    ctx.login_limiter.reset(f"login:{ip}:{body.email}")
    return await _issue_session(ctx, user, response, request)


@router.post("/forgot-password")
async def forgot_password(body: ForgotPasswordBody, request: Request, ctx: Ctx) -> dict[str, Any]:
    """Create and deliver a password reset OTP for a registered email address."""
    ip = _client_ip(request)
    for limiter, key in ((ctx.password_reset_ip_limiter, f"password-reset-ip:{ip}"),
                         (ctx.password_reset_limiter, f"password-reset:{body.email}")):
        allowed, retry = limiter.hit(key)
        if not allowed:
            raise RateLimited("too many password reset attempts; try again shortly", retry_after=retry)

    async with ctx.sessions() as session:
        user = await session.scalar(select(User).where(User.email == body.email))
        if user is None:
            raise NotFound("email not registered", code="email_not_registered")
        await session.execute(update(PasswordResetChallenge).where(
            PasswordResetChallenge.user_id == user.id,
            PasswordResetChallenge.consumed_at.is_(None),
        ).values(consumed_at=utcnow()))
        otp = f"{secrets.randbelow(1_000_000):06d}"
        salt = secrets.token_urlsafe(18)
        challenge = PasswordResetChallenge(
            user_id=user.id,
            email=body.email,
            code_salt=salt,
            code_hash=hash_otp(otp, salt),
            expires_at=utcnow() + timedelta(minutes=ctx.settings.password_reset_otp_ttl_minutes),
        )
        session.add(challenge)
        await session.commit()
    challenge_id = challenge.id
    try:
        await ctx.mailer.send_password_reset_otp(body.email, otp, ctx.settings.password_reset_otp_ttl_minutes)
    except Exception:
        log.exception("password reset email delivery failed")
        async with ctx.sessions() as session:
            row = await session.get(PasswordResetChallenge, challenge_id)
            if row is not None:
                row.consumed_at = utcnow()
                await session.commit()
        raise AppError(
            "password reset email could not be sent; check the SMTP configuration and try again",
            code="email_delivery_failed",
            status_code=503,
        ) from None
    return {"ok": True, "message": "verification code sent"}


@router.post("/verify-password-reset-otp")
async def verify_password_reset_otp(body: VerifyOtpBody, ctx: Ctx) -> dict[str, Any]:
    async with ctx.sessions() as session:
        challenge = await session.scalar(select(PasswordResetChallenge).where(
            PasswordResetChallenge.email == body.email,
            PasswordResetChallenge.consumed_at.is_(None),
            PasswordResetChallenge.verified_at.is_(None),
        ).order_by(PasswordResetChallenge.created_at.desc()))
        if (challenge is None or challenge.expires_at <= utcnow()
                or challenge.attempts >= ctx.settings.password_reset_max_attempts):
            raise Unauthorized("invalid or expired verification code", code="invalid_reset_otp")
        challenge.attempts += 1
        if not verify_otp(challenge.code_hash, body.otp, challenge.code_salt):
            await session.commit()
            raise Unauthorized("invalid or expired verification code", code="invalid_reset_otp")
        reset_token = secrets.token_urlsafe(32)
        challenge.reset_token_hash = hash_refresh_token(reset_token)
        challenge.verified_at = utcnow()
        await session.commit()
    return {"ok": True, "reset_token": reset_token}


@router.post("/reset-password")
async def reset_password(body: ResetPasswordBody, ctx: Ctx) -> dict[str, Any]:
    async with ctx.sessions() as session:
        challenge = await session.scalar(select(PasswordResetChallenge).where(
            PasswordResetChallenge.reset_token_hash == hash_refresh_token(body.reset_token),
            PasswordResetChallenge.consumed_at.is_(None),
        ))
        if challenge is None or challenge.verified_at is None or challenge.expires_at <= utcnow():
            raise Unauthorized("invalid or expired password reset session", code="invalid_reset_token")
        user = await session.get(User, challenge.user_id)
        if user is None:
            raise Unauthorized("invalid password reset session", code="invalid_reset_token")
        user.password_hash = hash_password(body.new_password)
        challenge.consumed_at = utcnow()
        # Password reset invalidates every outstanding refresh session for this user.
        await session.execute(update(RefreshToken).where(
            RefreshToken.user_id == user.id,
            RefreshToken.revoked_at.is_(None),
        ).values(revoked_at=utcnow()))
        await session.commit()
    return {"ok": True, "message": "Password updated. You can now sign in."}


@router.post("/refresh")
async def refresh(request: Request, response: Response, ctx: Ctx) -> dict[str, Any]:
    raw = request.cookies.get(REFRESH_COOKIE)
    if not raw:
        raise Unauthorized("no refresh token", code="no_refresh_token")
    digest = hash_refresh_token(raw)
    async with ctx.sessions() as session:
        token = await session.scalar(select(RefreshToken).where(RefreshToken.token_hash == digest))
        if token is None:
            raise Unauthorized("invalid refresh token", code="invalid_refresh_token")
        if token.revoked_at is not None:
            # Reuse of a rotated token: assume theft and revoke the whole family.
            await session.execute(update(RefreshToken).where(RefreshToken.family_id == token.family_id)
                                  .values(revoked_at=utcnow()))
            await session.commit()
            raise Unauthorized("refresh token reuse detected; please sign in again", code="refresh_reuse")
        if token.expires_at <= utcnow():
            raise Unauthorized("refresh token expired", code="refresh_expired")
        token.revoked_at = utcnow()
        user = await session.get(User, token.user_id)
        family = token.family_id
        await session.commit()
    if user is None:
        raise Unauthorized("user no longer exists")
    return await _issue_session(ctx, user, response, request, family_id=family)


@router.post("/logout")
async def logout(request: Request, response: Response, ctx: Ctx) -> dict[str, Any]:
    raw = request.cookies.get(REFRESH_COOKIE)
    if raw:
        async with ctx.sessions() as session:
            token = await session.scalar(select(RefreshToken).where(
                RefreshToken.token_hash == hash_refresh_token(raw)))
            if token is not None:
                await session.execute(update(RefreshToken).where(RefreshToken.family_id == token.family_id)
                                      .values(revoked_at=utcnow()))
                await session.commit()
    response.delete_cookie(REFRESH_COOKIE, path="/api/auth")
    return {"ok": True}


@router.get("/me")
async def me(user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    data = user_public(user)
    data["auth_required"] = ctx.settings.auth_required
    return data


# --------------------------------------------------------------------------- OAuth2
def _oauth_client(ctx: AppContext) -> httpx.AsyncClient:
    if ctx.oauth_transport is not None:
        return httpx.AsyncClient(transport=ctx.oauth_transport, timeout=15)
    return httpx.AsyncClient(timeout=15)


def _safe_next(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/index.html"


def _require_oauth(ctx: AppContext, provider: str) -> tuple[str, str]:
    if provider not in OAUTH_PROVIDERS:
        raise NotFound(f"unknown OAuth provider '{provider}'")
    s = ctx.settings
    if provider == "google" and s.google_client_id and s.google_client_secret:
        return s.google_client_id, s.google_client_secret.get_secret_value()
    if provider == "github" and s.github_client_id and s.github_client_secret:
        return s.github_client_id, s.github_client_secret.get_secret_value()
    label = "Google" if provider == "google" else "GitHub"
    env = provider.upper()
    raise NotConfigured(
        f"{label} sign-in is not configured on this server. Set {env}_CLIENT_ID and {env}_CLIENT_SECRET.",
        code="oauth_not_configured")


def _redirect_uri(ctx: AppContext, provider: str) -> str:
    return f"{ctx.settings.public_base_url.rstrip('/')}/api/auth/oauth/{provider}/callback"


@router.get("/oauth/{provider}/start", response_model=None)
async def oauth_start(provider: str, ctx: Ctx, format: str = Query("redirect"),
                      next: str | None = Query(None)) -> Response:
    client_id, _ = _require_oauth(ctx, provider)
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params: dict[str, str] = {
        "client_id": client_id, "redirect_uri": _redirect_uri(ctx, provider), "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    }
    if provider == "google":
        params.update({"response_type": "code", "scope": "openid email profile", "prompt": "select_account"})
        url = f"{ctx.settings.google_auth_url}?{urlencode(params)}"
    else:
        params.update({"scope": "read:user user:email", "allow_signup": "true"})
        url = f"{ctx.settings.github_auth_url}?{urlencode(params)}"
    cookie = ctx.tokens.sign_state({"state": state, "provider": provider, "verifier": verifier,
                                    "next": _safe_next(next)})
    response: Response = JSONResponse({"url": url}) if format == "json" else RedirectResponse(url, 302)
    response.set_cookie(OAUTH_STATE_COOKIE, cookie, httponly=True, samesite="lax", max_age=600,
                        path="/api/auth/oauth", secure=ctx.settings.public_base_url.startswith("https"))
    return response


async def _google_profile(ctx: AppContext, client: httpx.AsyncClient, code: str, verifier: str,
                          client_id: str, secret: str) -> dict[str, Any]:
    token_resp = await client.post(ctx.settings.google_token_url, data={
        "client_id": client_id, "client_secret": secret, "code": code, "code_verifier": verifier,
        "grant_type": "authorization_code", "redirect_uri": _redirect_uri(ctx, "google")},
        headers={"Accept": "application/json"})
    if token_resp.status_code >= 400:
        raise AppError("Google token exchange failed", code="oauth_exchange_failed")
    access = token_resp.json().get("access_token")
    info = await client.get(ctx.settings.google_userinfo_url, headers={"Authorization": f"Bearer {access}"})
    if info.status_code >= 400:
        raise AppError("Google profile lookup failed", code="oauth_profile_failed")
    data = info.json()
    return {"id": str(data.get("sub")), "email": data.get("email"),
            "email_verified": bool(data.get("email_verified")), "name": data.get("name") or "",
            "avatar": data.get("picture")}


async def _github_profile(ctx: AppContext, client: httpx.AsyncClient, code: str, verifier: str,
                          client_id: str, secret: str) -> dict[str, Any]:
    token_resp = await client.post(ctx.settings.github_token_url, data={
        "client_id": client_id, "client_secret": secret, "code": code, "code_verifier": verifier,
        "redirect_uri": _redirect_uri(ctx, "github")}, headers={"Accept": "application/json"})
    if token_resp.status_code >= 400 or "access_token" not in token_resp.json():
        raise AppError("GitHub token exchange failed", code="oauth_exchange_failed")
    headers = {"Authorization": f"Bearer {token_resp.json()['access_token']}",
               "Accept": "application/vnd.github+json"}
    user_resp = await client.get(f"{ctx.settings.github_api_url}/user", headers=headers)
    if user_resp.status_code >= 400:
        raise AppError("GitHub profile lookup failed", code="oauth_profile_failed")
    data = user_resp.json()
    email, verified = data.get("email"), False
    emails_resp = await client.get(f"{ctx.settings.github_api_url}/user/emails", headers=headers)
    if emails_resp.status_code < 400:
        for item in emails_resp.json():
            if item.get("primary"):
                email, verified = item.get("email"), bool(item.get("verified"))
    return {"id": str(data.get("id")), "email": email, "email_verified": verified,
            "name": data.get("name") or data.get("login") or "", "avatar": data.get("avatar_url")}


async def _upsert_oauth_user(ctx: AppContext, provider: str, profile: dict[str, Any]) -> User:
    async with ctx.sessions() as session:
        account = await session.scalar(select(OAuthAccount).where(
            OAuthAccount.provider == provider, OAuthAccount.provider_user_id == profile["id"]))
        if account is not None:
            user = await session.get(User, account.user_id)
            if user is not None:
                return user
        email = (profile.get("email") or "").lower() or None
        user = None
        if email and profile.get("email_verified"):
            user = await session.scalar(select(User).where(User.email == email))
        if user is None:
            taken = email and await session.scalar(select(User).where(User.email == email)) is not None
            user = User(email=None if taken else email, name=profile.get("name") or (email or provider),
                        avatar_url=profile.get("avatar"))
            session.add(user)
            await session.flush()
        session.add(OAuthAccount(user_id=user.id, provider=provider, provider_user_id=profile["id"], email=email))
        await session.commit()
        await session.refresh(user)
        return user


@router.get("/oauth/{provider}/callback", response_model=None)
async def oauth_callback(provider: str, request: Request, ctx: Ctx, code: str | None = None,
                         state: str | None = None, error: str | None = None) -> Response:
    try:
        client_id, secret = _require_oauth(ctx, provider)
    except NotConfigured as exc:
        return JSONResponse({"detail": exc.message, "code": exc.code}, status_code=501)

    def fail(reason: str) -> RedirectResponse:
        resp = RedirectResponse(f"/auth.html?error={reason}", 302)
        resp.delete_cookie(OAUTH_STATE_COOKIE, path="/api/auth/oauth")
        return resp

    if error or not code or not state:
        return fail(error or "missing_code")
    try:
        stored = ctx.tokens.verify_state(request.cookies.get(OAUTH_STATE_COOKIE, ""))
    except TokenError:
        return fail("invalid_state")
    if not secrets.compare_digest(str(stored.get("state")), state) or stored.get("provider") != provider:
        return fail("invalid_state")
    try:
        async with _oauth_client(ctx) as client:
            fetch = _google_profile if provider == "google" else _github_profile
            profile = await fetch(ctx, client, code, str(stored["verifier"]), client_id, secret)
    except (AppError, httpx.HTTPError, ValueError) as exc:
        log.warning("oauth %s failed: %s", provider, exc)
        return fail("oauth_failed")
    user = await _upsert_oauth_user(ctx, provider, profile)
    target = _safe_next(stored.get("next"))
    response = RedirectResponse(f"{target}{'&' if '?' in target else '?'}auth=success", 302)
    response.delete_cookie(OAUTH_STATE_COOKIE, path="/api/auth/oauth")
    await _issue_session(ctx, user, response, request)
    return response
