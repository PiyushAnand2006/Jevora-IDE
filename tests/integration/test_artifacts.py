"""Run artifact serving: screenshots render, paths are traversal-guarded."""

import pytest


@pytest.mark.asyncio
async def test_artifacts_serve_and_guard(harness) -> None:
    client = harness.client
    settings = harness.ctx.settings
    run_dir = settings.data_dir / "artifacts" / "run_test"
    run_dir.mkdir(parents=True, exist_ok=True)
    png = run_dir / "shot.png"
    # 1x1 transparent PNG
    png.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d49444154789c626001000000ffff030000060005"
        "57bfabd40000000049454e44ae426082"))

    ok = await client.get("/api/artifacts", params={"path": "run_test/shot.png"})
    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("image/png")
    assert ok.content.startswith(b"\x89PNG")

    missing = await client.get("/api/artifacts", params={"path": "run_test/nope.png"})
    assert missing.status_code == 404

    for bad in ("../secret.txt", "/etc/passwd", "C:/Windows/win.ini", ""):
        blocked = await client.get("/api/artifacts", params={"path": bad})
        assert blocked.status_code in (404, 422), bad

    # A file inside the data dir but OUTSIDE artifacts/ must not be reachable.
    outside = settings.data_dir / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    leak = await client.get("/api/artifacts", params={"path": "../outside.txt"})
    assert leak.status_code == 422
    leak2 = await client.get("/api/artifacts", params={"path": "outside.txt"})
    assert leak2.status_code in (404, 422)
