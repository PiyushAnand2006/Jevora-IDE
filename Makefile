# Jevora IDE developer tasks. On Windows run these from Git Bash, or use the commands directly.
PY ?= $(if $(wildcard .venv/Scripts/python.exe),.venv/Scripts/python.exe,$(if $(wildcard .venv/bin/python),.venv/bin/python,python3))

.PHONY: help venv install dev test test-fast test-e2e lint typecheck check sandbox-image playwright eval \
        finetune-export api-docs docker-up docker-down clean

help:
	@echo "make install        create .venv and install dependencies"
	@echo "make dev            run the backend with reload on http://127.0.0.1:8000"
	@echo "make test           run the full test suite"
	@echo "make check          ruff + mypy + tests"
	@echo "make sandbox-image  build the Docker sandbox image"
	@echo "make playwright     install Chromium for the screen-control agent"
	@echo "make eval           evaluate decision engines (python -m backend.decision.eval)"

venv:
	python -m venv .venv

install: venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements-dev.txt

dev:
	$(PY) -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000

test:
	$(PY) -m pytest tests -q

test-fast:
	$(PY) -m pytest tests/unit -q

test-e2e:
	$(PY) -m pytest tests/e2e -q

lint:
	$(PY) -m ruff check backend tests

typecheck:
	$(PY) -m mypy backend

check: lint typecheck test

sandbox-image:
	docker build -t jevora-sandbox:latest -f docker/sandbox.Dockerfile docker

playwright:
	$(PY) -m playwright install chromium

eval:
	$(PY) -m backend.decision.eval

finetune-export:
	$(PY) -m backend.decision.finetune export

api-docs:
	$(PY) scripts/gen_api_docs.py

docker-up:
	docker compose up --build -d

docker-down:
	docker compose down

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache
