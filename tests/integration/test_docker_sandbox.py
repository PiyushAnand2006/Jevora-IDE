"""Docker sandbox tests (skipped automatically when no Docker daemon is reachable)."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.sandbox.docker_sandbox import DockerSandbox, image_exists


def _docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"], capture_output=True,
                              timeout=10, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = [pytest.mark.docker, pytest.mark.skipif(not _docker_ok(), reason="Docker daemon not available")]
IMAGE = "jevora-sandbox:latest"


@pytest.fixture
async def sandbox(tmp_path: Path):  # type: ignore[no-untyped-def]
    if not await image_exists(IMAGE):
        pytest.skip(f"{IMAGE} not built (run `make sandbox-image`)")
    ws = tmp_path / "ws"
    ws.mkdir()
    box = DockerSandbox(f"jevora-test-{tmp_path.name[-10:].lower()}", ws, IMAGE, memory="512m", pids_limit=128)
    yield box
    await box.stop()


async def test_exec_runs_in_container_with_workspace_mounted(sandbox: DockerSandbox) -> None:
    (sandbox.workspace / "hello.py").write_text("print('from container')\n", encoding="utf-8")
    result = await sandbox.exec("python hello.py && cat /etc/os-release | head -1 && pwd")
    assert result.ok and "from container" in result.stdout and "/workspace" in result.stdout
    await sandbox.exec("echo written-inside > out.txt")
    assert (sandbox.workspace / "out.txt").read_text().strip() == "written-inside"


async def test_network_disabled_by_default(sandbox: DockerSandbox) -> None:
    result = await sandbox.exec("python -c \"import socket; socket.create_connection(('1.1.1.1', 53), 3)\"",
                                timeout=20)
    assert result.exit_code != 0


async def test_timeout_kills_process(sandbox: DockerSandbox) -> None:
    result = await sandbox.exec("sleep 60", timeout=2)
    assert result.timed_out and result.exit_code != 0
    ps = await sandbox.exec("ps -eo args")
    assert "sleep 60" not in ps.stdout


async def test_cancel_kills_in_container_process(sandbox: DockerSandbox) -> None:
    await sandbox.ensure_started()
    task = asyncio.create_task(sandbox.exec("sleep 120", timeout=200))
    await asyncio.sleep(2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.5)
    ps = await sandbox.exec("ps -eo args")
    assert "sleep 120" not in ps.stdout


async def test_resource_limits_applied(sandbox: DockerSandbox) -> None:
    await sandbox.ensure_started()
    result = subprocess.run(["docker", "inspect", "-f", "{{.HostConfig.PidsLimit}} {{.HostConfig.NetworkMode}} "
                             "{{.HostConfig.Memory}}", sandbox.name], capture_output=True, text=True, check=False)
    pids, network, memory = result.stdout.split()
    assert pids == "128" and network == "none" and int(memory) == 512 * 1024 * 1024


async def test_pytest_available_in_image(sandbox: DockerSandbox) -> None:
    (sandbox.workspace / "test_x.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    result = await sandbox.exec("python -m pytest -q -p no:cacheprovider")
    assert result.ok and "1 passed" in result.stdout
