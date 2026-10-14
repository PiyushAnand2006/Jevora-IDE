# Jevora sandbox image: what agent commands and tests run inside.
# Network is disabled at runtime by default, so common tooling is preinstalled here.
FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ripgrep procps coreutils ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
      pytest==8.* pytest-asyncio flask fastapi httpx requests uvicorn \
      ruff mypy pyflakes radon pandas numpy

WORKDIR /workspace
CMD ["sleep", "infinity"]
