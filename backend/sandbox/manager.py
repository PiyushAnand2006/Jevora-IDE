"""Sandbox selection and lifecycle: Docker by default, flagged local fallback, per-run reuse, user VMs."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings
from backend.core.errors import AppError, NotFound, SandboxUnavailable
from backend.core.ids import iso, new_id
from backend.core.logging import get_logger
from backend.db.models import VM
from backend.sandbox.base import Sandbox, SandboxError
from backend.sandbox.docker_sandbox import (
    DockerSandbox,
    build_sandbox_image,
    docker,
    docker_version,
    gvisor_available,
    image_exists,
)
from backend.sandbox.local_sandbox import LocalSandbox

log = get_logger(__name__)


class SandboxManager:
    def __init__(self, settings: Settings, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.settings = settings
        self.sessions = sessions
        self._docker: tuple[float, dict[str, Any]] | None = None
        self._sandboxes: dict[str, Sandbox] = {}
        self._run_keys: dict[str, str] = {}
        self._image_lock = asyncio.Lock()
        self._image: str | None = None

    # ------------------------------------------------------------------ capability detection
    async def docker_info(self, refresh: bool = False) -> dict[str, Any]:
        if self._docker is not None and not refresh and time.monotonic() - self._docker[0] < 30:
            return self._docker[1]
        version = await docker_version()
        info: dict[str, Any] = {"available": version is not None, "version": version, "gvisor": False}
        if version is not None:
            info["gvisor"] = await gvisor_available()
        self._docker = (time.monotonic(), info)
        return info

    async def resolve_kind(self) -> str:
        mode = self.settings.sandbox_mode
        if mode == "local":
            if not self.settings.local_unsafe_exec:
                raise SandboxUnavailable("SANDBOX_MODE=local requires LOCAL_UNSAFE_EXEC=true (no isolation)")
            return "local"
        info = await self.docker_info()
        if info["available"]:
            return "docker"
        if mode == "auto" and self.settings.local_unsafe_exec:
            return "local"
        raise SandboxUnavailable(
            "Docker is not available, so commands cannot run in the sandbox. Start Docker, or set "
            "LOCAL_UNSAFE_EXEC=true to run commands directly on this machine (development only).")

    async def status(self) -> dict[str, Any]:
        info = await self.docker_info()
        try:
            kind: str | None = await self.resolve_kind()
            reason = None
        except SandboxUnavailable as exc:
            kind, reason = None, exc.message
        return {
            "ok": kind is not None, "available": kind is not None, "kind": kind, "reason": reason,
            "mode": self.settings.sandbox_mode, "local_unsafe_exec": self.settings.local_unsafe_exec,
            "docker": info, "image": self._image or self.settings.sandbox_image,
            "runtime": self._runtime(info), "network_default": self.settings.sandbox_network,
            "active": len(self._sandboxes),
        }

    def _runtime(self, info: dict[str, Any]) -> str | None:
        if self.settings.sandbox_runtime:
            return self.settings.sandbox_runtime
        return "runsc" if info.get("gvisor") else None

    async def ensure_image(self) -> str:
        async with self._image_lock:
            if self._image is not None:
                return self._image
            image = self.settings.sandbox_image
            if await image_exists(image) or await build_sandbox_image(image):
                self._image = image
                return image
            base = self.settings.sandbox_base_image
            log.warning("falling back to base image %s (pytest/flask may be missing)", base)
            if not await image_exists(base):
                pulled = await docker("pull", base, timeout=900)
                if pulled.exit_code != 0:
                    raise SandboxUnavailable(f"could not obtain a sandbox image: {pulled.stderr[-300:]}")
            self._image = base
            return base

    # ------------------------------------------------------------------ factories
    async def _make(self, key: str, container: str, workspace: Path, network: bool) -> Sandbox:
        existing = self._sandboxes.get(key)
        if existing is not None:
            return existing
        kind = await self.resolve_kind()
        sandbox: Sandbox
        if kind == "docker":
            info = await self.docker_info()
            sandbox = DockerSandbox(container, workspace, await self.ensure_image(), runtime=self._runtime(info),
                                    cpus=self.settings.sandbox_cpus, memory=self.settings.sandbox_memory,
                                    pids_limit=self.settings.sandbox_pids_limit, network=network,
                                    labels={"jevora.key": key})
        else:
            sandbox = LocalSandbox(container, workspace)
        try:
            await sandbox.ensure_started()
        except SandboxError as exc:
            raise SandboxUnavailable(str(exc)) from exc
        self._sandboxes[key] = sandbox
        return sandbox

    async def selected_vm(self, user_id: str) -> VM | None:
        async with self.sessions() as session:
            vm: VM | None = await session.scalar(select(VM).where(VM.user_id == user_id, VM.selected))
            return vm

    async def for_run(self, run_id: str, user_id: str, workspace: Path) -> Sandbox:
        if run_id in self._run_keys:
            return self._sandboxes[self._run_keys[run_id]]
        vm = await self.selected_vm(user_id)
        if vm is not None and vm.status != "error":
            sandbox = await self._make(f"vm:{vm.id}", vm.container_name or f"jevora-vm-{vm.id[-12:]}", workspace,
                                       vm.network)
            await self._set_vm_status(vm.id, "running")
            self._run_keys[run_id] = f"vm:{vm.id}"
            return sandbox
        key = f"run:{run_id}"
        sandbox = await self._make(key, f"jevora-run-{run_id[-12:]}", workspace, self.settings.sandbox_network)
        self._run_keys[run_id] = key
        return sandbox

    async def for_terminal(self, user_id: str, workspace: Path, vm_id: str | None = None) -> Sandbox:
        vm: VM | None = None
        async with self.sessions() as session:
            if vm_id:
                vm = await session.get(VM, vm_id)
                if vm is None or vm.user_id != user_id:
                    raise NotFound("VM not found")
            else:
                vm = await session.scalar(select(VM).where(VM.user_id == user_id, VM.selected))
        if vm is not None:
            sandbox = await self._make(f"vm:{vm.id}", vm.container_name or f"jevora-vm-{vm.id[-12:]}", workspace,
                                       vm.network)
            await self._set_vm_status(vm.id, "running")
            return sandbox
        return await self._make(f"terminal:{user_id}", f"jevora-term-{user_id[-12:]}", workspace,
                                self.settings.sandbox_network)

    async def kill_run(self, run_id: str) -> None:
        key = self._run_keys.get(run_id)
        sandbox = self._sandboxes.get(key) if key else None
        if sandbox is not None:
            await sandbox.kill_all()

    async def release_run(self, run_id: str) -> None:
        key = self._run_keys.pop(run_id, None)
        if key is None or not key.startswith("run:"):
            return  # VM sandboxes outlive runs
        sandbox = self._sandboxes.pop(key, None)
        if sandbox is not None:
            try:
                await sandbox.stop()
            except Exception:
                log.exception("failed to stop sandbox for %s", run_id)

    async def shutdown(self) -> None:
        for key, sandbox in list(self._sandboxes.items()):
            if key.startswith(("run:", "terminal:")):
                try:
                    await sandbox.stop()
                except Exception:
                    log.exception("failed to stop sandbox %s", key)
        self._sandboxes.clear()

    @property
    def active_count(self) -> int:
        return len(self._sandboxes)

    # ------------------------------------------------------------------ VMs (user-managed sandboxes)
    async def _set_vm_status(self, vm_id: str, status: str) -> None:
        async with self.sessions() as session:
            await session.execute(update(VM).where(VM.id == vm_id).values(status=status))
            await session.commit()

    @staticmethod
    def vm_public(vm: VM) -> dict[str, Any]:
        return {"id": vm.id, "name": vm.name, "kind": vm.kind, "status": vm.status, "selected": vm.selected,
                "network": vm.network, "image": vm.image, "container": vm.container_name,
                "created_at": iso(vm.created_at)}

    async def list_vms(self, user_id: str) -> dict[str, Any]:
        async with self.sessions() as session:
            vms = list(await session.scalars(select(VM).where(VM.user_id == user_id).order_by(VM.created_at)))
        status = await self.status()
        default = {"id": "default", "name": "Per-run sandbox", "kind": status["kind"] or "unavailable",
                   "status": "ready" if status["available"] else "unavailable",
                   "selected": not any(v.selected for v in vms), "network": self.settings.sandbox_network,
                   "image": status["image"], "container": None, "created_at": None}
        running = sum(1 for v in vms if v.status == "running") + sum(
            1 for k in self._sandboxes if k.startswith("run:"))
        return {"vms": [default, *[self.vm_public(v) for v in vms]], "running": running, "sandbox": status}

    async def create_vm(self, user_id: str, name: str, network: bool = False) -> dict[str, Any]:
        kind = await self.resolve_kind()
        vm_id = new_id("vm_")
        vm = VM(id=vm_id, user_id=user_id, name=name.strip()[:120] or "Sandbox", kind=kind, status="stopped",
                container_name=f"jevora-vm-{vm_id[-12:]}",
                image=self.settings.sandbox_image if kind == "docker" else None, network=network)
        async with self.sessions() as session:
            session.add(vm)
            await session.commit()
            await session.refresh(vm)
        return self.vm_public(vm)

    async def _owned_vm(self, user_id: str, vm_id: str) -> VM:
        async with self.sessions() as session:
            vm = await session.get(VM, vm_id)
        if vm is None or vm.user_id != user_id:
            raise NotFound("VM not found")
        return vm

    async def start_vm(self, user_id: str, vm_id: str, workspace: Path) -> dict[str, Any]:
        vm = await self._owned_vm(user_id, vm_id)
        try:
            await self._make(f"vm:{vm.id}", vm.container_name or f"jevora-vm-{vm.id[-12:]}", workspace, vm.network)
        except SandboxUnavailable:
            await self._set_vm_status(vm.id, "error")
            raise
        await self._set_vm_status(vm.id, "running")
        return self.vm_public(await self._owned_vm(user_id, vm_id))

    async def stop_vm(self, user_id: str, vm_id: str) -> dict[str, Any]:
        vm = await self._owned_vm(user_id, vm_id)
        sandbox = self._sandboxes.pop(f"vm:{vm.id}", None)
        if sandbox is not None:
            await sandbox.stop()
        await self._set_vm_status(vm.id, "stopped")
        return self.vm_public(await self._owned_vm(user_id, vm_id))

    async def select_vm(self, user_id: str, vm_id: str) -> dict[str, Any]:
        async with self.sessions() as session:
            await session.execute(update(VM).where(VM.user_id == user_id).values(selected=False))
            if vm_id != "default":
                vm = await session.get(VM, vm_id)
                if vm is None or vm.user_id != user_id:
                    raise NotFound("VM not found")
                vm.selected = True
            await session.commit()
        return await self.list_vms(user_id)

    async def delete_vm(self, user_id: str, vm_id: str) -> None:
        if vm_id == "default":
            raise AppError("the default sandbox cannot be deleted", code="invalid_vm", status_code=422)
        await self.stop_vm(user_id, vm_id)
        async with self.sessions() as session:
            vm = await session.get(VM, vm_id)
            if vm is not None:
                await session.delete(vm)
                await session.commit()
