"""System endpoints: health, feature flags, Prometheus metrics, token/cost usage."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select, text

from backend import __version__
from backend.api.deps import Ctx, CurrentUser
from backend.app_context import AppContext
from backend.core.errors import AppError
from backend.core.ids import iso, utcnow
from backend.core.proc import run_process
from backend.db.models import LLMCall

router = APIRouter(tags=["system"])


async def _provider_reachability(ctx: AppContext, user_id: str) -> list[dict[str, Any]]:
    providers = await ctx.providers.list_for(user_id)

    async def probe(p: Any) -> dict[str, Any]:
        adapter = ctx.providers.adapter_for(p)
        try:
            models = await asyncio.wait_for(adapter.list_models(), timeout=4)
            return {"id": p.id, "name": p.name, "kind": p.kind, "reachable": True, "models": len(models),
                    "circuit": ctx.router.breakers.get(p.id).state}
        except (Exception, TimeoutError) as exc:
            return {"id": p.id, "name": p.name, "kind": p.kind, "reachable": False,
                    "error": str(exc)[:200] or type(exc).__name__, "circuit": ctx.router.breakers.get(p.id).state}

    return list(await asyncio.gather(*(probe(p) for p in providers)))


async def build_health(ctx: AppContext, user_id: str, probe_providers: bool = True) -> dict[str, Any]:
    db_ok = True
    try:
        async with ctx.sessions() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    health: dict[str, Any] = {
        "status": "ok" if db_ok else "degraded",
        "version": __version__,
        "time": iso(utcnow()),
        "database": {"ok": db_ok},
        "auth_required": ctx.settings.auth_required,
        "providers": await _provider_reachability(ctx, user_id) if probe_providers else [],
    }
    for name, component in ctx.extras.get("health_components", {}).items():
        try:
            health[name] = await component(user_id)
        except Exception as exc:  # a broken component must not break health
            health[name] = {"ok": False, "error": str(exc)[:300]}
    return health


@router.get("/api/system/health")
async def system_health(user: CurrentUser, ctx: Ctx, probe: bool = Query(True)) -> dict[str, Any]:
    return await build_health(ctx, user.id, probe_providers=probe)


@router.get("/api/features")
async def features(ctx: Ctx) -> dict[str, Any]:
    flags: dict[str, Any] = {
        "web_search": ctx.settings.web_search_enabled,
        "web_search_configured": ctx.web_search.status()["configured"],
        "web_search_provider": ctx.settings.web_search_provider if ctx.settings.web_search_enabled else None,
        "auth_required": ctx.settings.auth_required,
        "oauth": ctx.settings.oauth_configured,
        "desktop_control": ctx.settings.desktop_control_enabled,
        "local_unsafe_exec": ctx.settings.local_unsafe_exec,
        "approval_policy": ctx.settings.approval_policy,
    }
    flags.update(ctx.extras.get("feature_flags", {}))
    return flags


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(ctx: Ctx) -> PlainTextResponse:
    return PlainTextResponse(ctx.metrics.render().decode("utf-8"),
                             media_type="text/plain; version=0.0.4; charset=utf-8")


@router.get("/api/usage")
async def usage(user: CurrentUser, ctx: Ctx, days: int = Query(30, ge=1, le=365),
                group_by: str = Query("model", pattern="^(model|provider|day|category)$")) -> dict[str, Any]:
    since = utcnow() - timedelta(days=days)
    column = {
        "model": LLMCall.model,
        "provider": LLMCall.provider_kind,
        "category": LLMCall.category,
        "day": func.date(LLMCall.created_at),
    }[group_by]
    async with ctx.sessions() as session:
        rows = await session.execute(
            select(column.label("key"), func.count(LLMCall.id), func.sum(LLMCall.prompt_tokens),
                   func.sum(LLMCall.completion_tokens), func.sum(LLMCall.cost_usd), func.avg(LLMCall.latency_ms),
                   func.sum(case((LLMCall.status == "error", 1), else_=0)))
            .where(LLMCall.user_id == user.id, LLMCall.created_at >= since)
            .group_by(column).order_by(func.sum(LLMCall.cost_usd).desc())
        )
        groups = [{"key": key, "calls": calls, "prompt_tokens": int(pt or 0), "completion_tokens": int(ct or 0),
                   "cost_usd": round(float(cost or 0), 6), "avg_latency_ms": round(float(lat or 0), 1),
                   "errors": int(errs or 0)} for key, calls, pt, ct, cost, lat, errs in rows.all()]
    return {
        "since": iso(since), "group_by": group_by, "groups": groups,
        "totals": {
            "calls": sum(g["calls"] for g in groups),
            "tokens": sum(g["prompt_tokens"] + g["completion_tokens"] for g in groups),
            "cost_usd": round(sum(g["cost_usd"] for g in groups), 6),
        },
    }


async def ping_url(client: httpx.AsyncClient, url: str) -> bool:
    try:
        response = await client.get(url, timeout=2)
        return response.status_code < 500
    except httpx.HTTPError:
        return False


# --------------------------------------------------------------------------- sandbox runtime (Docker / WSL)
#
# The desktop app needs a command-isolation runtime for agents and the terminal.
# On Windows that means Docker Desktop (WSL2 backend) and/or WSL itself. These
# endpoints detect what is installed and kick off the user-approved setup
# actions; downloads/opening the Docker site happen in the user's browser.

DOCKER_DOWNLOAD_URL = "https://www.docker.com/products/docker-desktop/"
WSL_DOC_URL = "https://learn.microsoft.com/en-us/windows/wsl/install"
_DOCKER_DESKTOP_CANDIDATES = (
    r"C:\Program Files\Docker\Docker\Docker Desktop.exe",
    r"C:\Program Files\Docker\Docker\frontend\Docker Desktop.exe",
)


def _docker_desktop_path() -> str | None:
    for candidate in _DOCKER_DESKTOP_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


async def _wsl_status() -> dict[str, Any]:
    """Detect WSL: feature enabled and at least one distro installed."""
    status: dict[str, Any] = {"available": False, "has_distro": False, "detail": ""}
    try:
        probe = await run_process(["wsl.exe", "--status"], timeout=15)
    except (OSError, ValueError):
        status["detail"] = "wsl.exe is not available on this system"
        return status
    if probe.exit_code != 0 and not probe.stdout.strip():
        status["detail"] = (probe.stderr or "WSL is not installed").strip()[:300]
        return status
    status["available"] = True
    listing = await run_process(["wsl.exe", "-l", "-q"], timeout=15)
    distros = [line.strip() for line in (listing.stdout or "").splitlines() if line.strip()
               and not line.startswith(("Windows Subsystem", "适用于"))]
    status["has_distro"] = listing.exit_code == 0 and bool(distros)
    if not status["has_distro"]:
        status["detail"] = "WSL is present but no Linux distribution is installed yet"
    return status


@router.get("/api/system/runtime")
async def runtime_status(user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    docker_version = await ctx.sandboxes.docker_info(refresh=True)
    wsl = await _wsl_status()
    desktop = _docker_desktop_path() if sys.platform == "win32" else None
    docker = {"installed": bool(docker_version.get("version")), "running": docker_version.get("available", False),
              "version": docker_version.get("version"), "desktop_path": desktop,
              "download_url": DOCKER_DOWNLOAD_URL}
    if docker["running"]:
        recommendation = "ready"
    elif sys.platform == "win32" and not wsl["available"]:
        recommendation = "wsl_install"
    elif docker["installed"]:
        recommendation = "docker_start"
    else:
        recommendation = "docker_install"
    return {"platform": sys.platform, "docker": docker, "wsl": wsl, "recommendation": recommendation,
            "sandbox": await ctx.sandboxes.status(), "docs": {"docker": DOCKER_DOWNLOAD_URL, "wsl": WSL_DOC_URL}}


class RuntimeAction(BaseModel):
    action: str = Field(pattern="^(wsl_install|wsl_update|docker_start)$")


@router.post("/api/system/runtime/enable")
async def runtime_enable(body: RuntimeAction, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    """Run one user-initiated setup step (WSL install/update or start Docker Desktop)."""
    if body.action == "docker_start":
        if sys.platform != "win32":
            raise AppError("starting Docker Desktop is only supported on Windows",
                           code="unsupported_platform", status_code=422)
        desktop = _docker_desktop_path()
        if desktop is None:
            raise AppError("Docker Desktop is not installed; download it from docker.com",
                           code="docker_not_installed", status_code=404)
        try:
            # Launch detached so the backend never owns the Docker Desktop process.
            creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            subprocess.Popen([desktop], cwd=str(Path(desktop).parent), creationflags=creationflags,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise AppError(f"could not launch Docker Desktop: {exc}", code="docker_launch_failed",
                           status_code=500) from exc
        return {"ok": True, "action": body.action,
                "message": "Docker Desktop is starting — this can take up to a minute."}

    argv = ["wsl.exe", "--install"] if body.action == "wsl_install" else ["wsl.exe", "--update"]
    try:
        result = await run_process(argv, timeout=600)
    except (OSError, ValueError) as exc:
        raise AppError(f"could not run wsl.exe: {exc}", code="wsl_failed", status_code=500) from exc
    # wsl.exe writes UTF-16LE on Windows; drop the NUL padding the decoder kept.
    message = (result.stdout or result.stderr or "").replace("\x00", "").strip()[-1500:]
    if result.exit_code == 0 and body.action == "wsl_install":
        message = message or "WSL components installed. A system restart may be required, then run this step again."
    return {"ok": result.exit_code == 0, "action": body.action, "exit_code": result.exit_code,
            "message": message or "done"}
