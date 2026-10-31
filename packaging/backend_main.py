"""Backend entry point used by the Electron shell (dev and packaged).

Reads JEVORA_HOST/JEVORA_PORT from the environment (the Electron backend
manager passes them; both default to the values uvicorn would use anyway)
and serves backend.main:app. Kept as a plain script so both the venv python
and the PyInstaller-frozen server share one entry point.
"""
from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    from backend.main import app

    host = os.environ.get("JEVORA_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("JEVORA_PORT", "8000"))
    except ValueError:
        port = 8000
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
