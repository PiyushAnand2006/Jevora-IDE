"""FastAPI application: lifespan, routers, CORS, static frontend.

Start with ``python -m uvicorn backend.main:app --reload`` and open http://127.0.0.1:8000.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope

from backend import __version__
from backend.api import (
    agents,
    attachments,
    auth,
    extras,
    mcp,
    providers,
    runs,
    system,
    terminal,
    vault,
    vms,
    workspace,
)
from backend.app_context import build_context
from backend.config import Settings, get_settings
from backend.core.errors import install_error_handlers
from backend.core.logging import configure_logging, get_logger

log = get_logger(__name__)


class FrontendFiles(StaticFiles):
    """Static frontend with no-cache headers so edits show up immediately in dev."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


def create_app(settings: Settings | None = None,
               http_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level, settings.log_json)
        ctx = await build_context(settings, http_transport)
        app.state.ctx = ctx
        await ctx.start()
        log.info("Jevora IDE backend %s ready (auth_required=%s)", __version__, settings.auth_required)
        try:
            yield
        finally:
            await ctx.aclose()

    app = FastAPI(title="Jevora IDE API", version=__version__, lifespan=lifespan,
                  description="Agent-native, local-first IDE backend (IDE Coder).")
    app.state.settings = settings
    install_error_handlers(app)
    app.add_middleware(
        CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
        allow_methods=["*"], allow_headers=["*"],
    )

    @app.middleware("http")
    async def _metrics(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        started = time.perf_counter()
        response = await call_next(request)
        ctx = getattr(request.app.state, "ctx", None)
        if ctx is not None and request.url.path.startswith("/api"):
            route = request.scope.get("route")
            path = getattr(route, "path", "unmatched")
            ctx.metrics.http_requests.labels(request.method, path, str(response.status_code)).inc()
        response.headers["X-Response-Time-Ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"
        return response

    for module in (auth, providers, runs, agents, workspace, terminal, vms, vault, extras, mcp, attachments,
                   system):
        app.include_router(module.router)

    if settings.frontend_dir.exists():
        app.mount("/", FrontendFiles(directory=settings.frontend_dir, html=True), name="frontend")
    return app


app = create_app()
