"""FastAPI dependencies: app context, current user (HTTP + WebSocket)."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request, WebSocket

from backend.app_context import AppContext
from backend.core.errors import Unauthorized
from backend.core.logging import user_id_var
from backend.core.security import TokenError, TokenExpired
from backend.db.models import User


def get_ctx(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx


Ctx = Annotated[AppContext, Depends(get_ctx)]


def _bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


async def resolve_user(ctx: AppContext, token: str | None) -> User:
    """Resolve the caller. A presented token must be valid; without one, local mode uses the local user."""
    if token:
        try:
            claims = ctx.tokens.decode_access(token)
        except TokenExpired as exc:
            raise Unauthorized("access token expired", code="token_expired") from exc
        except TokenError as exc:
            raise Unauthorized("invalid access token", code="invalid_token") from exc
        async with ctx.sessions() as session:
            user = await session.get(User, claims["sub"])
        if user is None:
            raise Unauthorized("user no longer exists", code="invalid_token")
        user_id_var.set(user.id)
        return user
    if ctx.settings.auth_required:
        raise Unauthorized("authentication required", code="auth_required")
    async with ctx.sessions() as session:
        user = await session.get(User, ctx.local_user_id)
    if user is None:  # pragma: no cover - created at startup
        raise Unauthorized("local user missing")
    user_id_var.set(user.id)
    return user


async def get_current_user(request: Request) -> User:
    ctx: AppContext = request.app.state.ctx
    return await resolve_user(ctx, _bearer(request.headers.get("authorization")))


CurrentUser = Annotated[User, Depends(get_current_user)]


async def ws_user(websocket: WebSocket) -> User:
    ctx: AppContext = websocket.app.state.ctx
    token = websocket.query_params.get("token") or _bearer(websocket.headers.get("authorization"))
    return await resolve_user(ctx, token)
