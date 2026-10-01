"""IDE Coder local backend: REST control plane + WebSocket run stream."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import ESCALATION_CONFIDENCE_THRESHOLD, FRONTEND_DIR, MAX_ITERATIONS, WORKSPACE_DIR
from .decision_engine import get_decision_engine
from .router import complete, list_models, resolve_model
from .schemas import AgentConfig, AgentRun, Decision, Evidence, Provider, StreamEvent, TaskCategory, TaskRun, TaskSpec, now
from .storage import load_providers, load_router_config, redact_secrets, save_providers, save_router_config, write_run_note
from .workspace_tools import list_files, read_file, run_agent_command, run_command, write_file

RUNS: dict[str, TaskRun] = {}
SUBSCRIBERS: dict[str, set[WebSocket]] = {}
EVENTS: dict[str, list[dict[str, Any]]] = {}


class ProviderInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str = ""
    kind: Literal["hosted", "local", "custom"] = "custom"


class RouterConfigInput(BaseModel):
    tiers: dict[str, dict[str, str | None]]


class FileWrite(BaseModel):
    content: str


class CommandInput(BaseModel):
    command: str = Field(min_length=1, max_length=4000)
    timeout: int = Field(default=60, ge=1, le=300)


def public_provider(provider: dict[str, Any]) -> dict[str, Any]:
    return {**provider, "api_key": "", "has_api_key": bool(provider.get("api_key"))}


def apply_coder_changes(response: str) -> tuple[str, list[str]]:
    """Apply only a bounded structured file payload returned by the coder model."""
    raw = response.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        payload = json.loads(raw)
        summary = str(payload.get("summary", "Changes applied."))
        files = payload.get("files", [])
        if not isinstance(files, list) or len(files) > 20:
            raise ValueError("The agent returned an invalid number of files")
        changed: list[str] = []
        for item in files:
            path, content = item.get("path"), item.get("content")
            if not isinstance(path, str) or not isinstance(content, str) or len(content) > 200_000:
                raise ValueError("The agent returned an invalid file change")
            write_file(path, content)
            changed.append(path)
        return summary, changed
    except (json.JSONDecodeError, AttributeError, ValueError) as exc:
        return f"Coder response was not applied: {exc}", []


async def collect_test_evidence(files_changed: list[str], tool_permissions: list[str] | None = None) -> Evidence:
    """Choose one conventional project test command and record real tool evidence."""
    command: str | None = None
    package = WORKSPACE_DIR / "package.json"
    if package.exists():
        try:
            if json.loads(package.read_text(encoding="utf-8")).get("scripts", {}).get("test"):
                command = "npm test -- --runInBand"
        except (OSError, json.JSONDecodeError):
            pass
    if not command and ((WORKSPACE_DIR / "pyproject.toml").exists() or (WORKSPACE_DIR / "pytest.ini").exists()):
        command = "pytest -q"
    if not command:
        return Evidence(files_changed=files_changed, exec_log_tail="No conventional test command was found.")
    result = await (run_agent_command(tool_permissions, command, timeout=120)
                    if tool_permissions is not None else run_command(command, timeout=120))
    log = (str(result["stdout"]) + "\n" + str(result["stderr"]))[-4000:]
    return Evidence(files_changed=files_changed, tests_run=1, test_passed=result["exit_code"] == 0, exec_log_tail=log)


def redact_event_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {key: redact_event_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_event_value(item) for item in value]
    return value


async def emit(run: TaskRun, event_type: str, payload: dict[str, Any]) -> None:
    safe_payload = redact_event_value(payload)
    event = StreamEvent(run_id=run.id, type=event_type, payload=safe_payload).model_dump(mode="json")
    EVENTS.setdefault(run.id, []).append(event)
    stale: list[WebSocket] = []
    for socket in SUBSCRIBERS.get(run.id, set()).copy():
        try:
            await socket.send_json(event)
        except Exception:
            stale.append(socket)
    for socket in stale:
        SUBSCRIBERS.get(run.id, set()).discard(socket)


async def run_agent(run: TaskRun) -> None:
    """Run a bounded planner → coder → tester graph driven by typed decisions."""
    engine = get_decision_engine()
    try:
        await emit(run, "run_started", {"task": run.spec.text, "mode": run.spec.mode})
        route = engine.route(run.spec.text, run.spec.mode)
        run.decisions.append(route)
        await emit(run, "decision", route.model_dump(mode="json"))
        category = TaskCategory(route.choice)
        provider, model = resolve_model(category)

        planner = AgentRun(config=AgentConfig(name="Planner", template="planner", tool_permissions=["workspace_read"]))
        run.agents.append(planner); planner.status = "running"; planner.model_used = model
        await emit(run, "agent_started", {"agent": planner.model_dump(mode="json")})
        plan = await complete(provider, model, "You are an IDE planning agent. Return a concise numbered implementation plan.", run.spec.text)
        planner.output = plan or "Plan the task, make the smallest safe changes, then validate them with available tests."
        planner.status = "done"; planner.finished_at = now()
        await emit(run, "agent_finished", {"agent": planner.model_dump(mode="json")})

        last_output = ""
        max_attempts = max(1, MAX_ITERATIONS)
        for attempt in range(max_attempts):
            coder_name = "Coder" if attempt == 0 else "Coder (retry)"
            coder = AgentRun(parent_id=planner.id, config=AgentConfig(name=coder_name, template="coder", tool_permissions=["workspace_read", "workspace_write", "terminal"]))
            run.agents.append(coder); coder.status = "running"; coder.model_used = model
            await emit(run, "subagent_called", {"parent_id": planner.id, "agent": coder.model_dump(mode="json")})
            await emit(run, "agent_started", {"agent": coder.model_dump(mode="json")})
            coder_prompt = f"Task: {run.spec.text}\n\nPlan:\n{planner.output}\n\nReturn ONLY JSON: {{\"summary\": \"what changed\", \"files\": [{{\"path\": \"relative/path\", \"content\": \"complete file content\"}}]}}. Include only files that need changing."
            model_response = await complete(provider, model, "You are a careful coding agent. Make minimal, complete file changes and return valid JSON only.", coder_prompt)
            if model_response:
                coder.output, changed_files = apply_coder_changes(model_response)
            else:
                changed_files = []
                coder.output = "No model provider with an API key is configured. Add a provider, then rerun this task to let the coding agent generate changes."
            coder.status = "done"; coder.finished_at = now(); last_output = coder.output
            await emit(run, "agent_token", {"agent_id": coder.id, "text": coder.output})
            await emit(run, "agent_finished", {"agent": coder.model_dump(mode="json")})

            tester = AgentRun(parent_id=coder.id, config=AgentConfig(name="Tester", template="tester", tool_permissions=["workspace_read", "terminal"]))
            run.agents.append(tester); tester.status = "running"; tester.model_used = model
            await emit(run, "subagent_called", {"parent_id": coder.id, "agent": tester.model_dump(mode="json")})
            evidence = await collect_test_evidence(changed_files, tester.config.tool_permissions)
            verdict = engine.verify(evidence)
            run.decisions.append(verdict)
            low_confidence = verdict.confidence < ESCALATION_CONFIDENCE_THRESHOLD
            tester.output = verdict.rationale; tester.status = "escalated" if verdict.escalate or low_confidence else "done"; tester.finished_at = now()
            await emit(run, "agent_finished", {"agent": tester.model_dump(mode="json")})
            await emit(run, "decision", verdict.model_dump(mode="json"))
            run.final_output = last_output + ("\n\nVerification: " + verdict.rationale)

            if low_confidence or verdict.escalate or verdict.choice == "escalate":
                run.status = "escalated"
                break
            if verdict.choice == "confirm_done":
                run.status = "done"
                break
            if verdict.choice in {"restart_same_agent", "reassign_new_agent"} and attempt + 1 < max_attempts:
                await emit(run, "node_status", {"status": "retrying", "attempt": attempt + 1, "verdict": verdict.choice})
                continue

            cap_verdict = Decision(kind="retry", choice="escalate", confidence=1.0, engine="orchestrator",
                                   rationale=f"Retry limit of {max_attempts} reached.", escalate=True)
            run.decisions.append(cap_verdict)
            run.status = "escalated"
            run.final_output += "\n\n" + cap_verdict.rationale
            await emit(run, "decision", cap_verdict.model_dump(mode="json"))
            break
    except Exception as exc:
        run.status = "failed"; run.final_output = f"Run failed: {exc}"
        await emit(run, "error", {"message": run.final_output})
    finally:
        run.finished_at = now()
        note = write_run_note(run.model_dump(mode="json"))
        await emit(run, "run_finished", {"status": run.status, "final_output": run.final_output, "vault_note": str(note)})


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield


app = FastAPI(title="IDE Coder Local API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "workspace": str(WORKSPACE_DIR), "max_iterations": MAX_ITERATIONS}


@app.post("/api/runs", status_code=202)
async def create_run(spec: TaskSpec) -> dict[str, Any]:
    run = TaskRun(spec=spec)
    RUNS[run.id] = run
    asyncio.create_task(run_agent(run))
    return run.model_dump(mode="json")


@app.get("/api/runs")
async def list_runs() -> list[dict[str, Any]]:
    return [run.model_dump(mode="json") for run in sorted(RUNS.values(), key=lambda item: item.created_at, reverse=True)]


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    if run_id not in RUNS: raise HTTPException(404, "Run not found")
    return RUNS[run_id].model_dump(mode="json")


@app.websocket("/api/runs/{run_id}/stream")
async def stream_run(websocket: WebSocket, run_id: str) -> None:
    if run_id not in RUNS:
        await websocket.close(code=4404); return
    await websocket.accept()
    for event in EVENTS.get(run_id, []):
        await websocket.send_json(event)
    SUBSCRIBERS.setdefault(run_id, set()).add(websocket)
    try:
        await websocket.send_json(StreamEvent(run_id=run_id, type="info", payload={"status": RUNS[run_id].status}).model_dump(mode="json"))
        while True: await websocket.receive_text()  # lets the client close or send pings
    except WebSocketDisconnect:
        pass
    finally:
        SUBSCRIBERS.get(run_id, set()).discard(websocket)


@app.get("/api/providers")
async def get_providers() -> list[dict[str, Any]]:
    return [public_provider(p) for p in load_providers()]


@app.post("/api/providers", status_code=201)
async def add_provider(data: ProviderInput) -> dict[str, Any]:
    provider = Provider(name=data.name, base_url=data.base_url, api_key=data.api_key, kind=data.kind).model_dump(mode="json")
    providers = load_providers(); providers.append(provider); save_providers(providers)
    return public_provider(provider)


@app.put("/api/providers/{provider_id}")
async def update_provider(provider_id: str, data: ProviderInput) -> dict[str, Any]:
    providers = load_providers()
    index = next((i for i, provider in enumerate(providers) if provider["id"] == provider_id), None)
    if index is None: raise HTTPException(404, "Provider not found")
    previous = providers[index]
    provider = Provider(id=provider_id, name=data.name, base_url=data.base_url,
                        api_key=data.api_key or previous.get("api_key", ""), kind=data.kind,
                        created_at=previous.get("created_at", now())).model_dump(mode="json")
    providers[index] = provider
    save_providers(providers)
    return public_provider(provider)


@app.get("/api/providers/{provider_id}/models")
async def get_provider_models(provider_id: str) -> dict[str, Any]:
    provider = next((item for item in load_providers() if item["id"] == provider_id), None)
    if provider is None: raise HTTPException(404, "Provider not found")
    return {"provider_id": provider_id, "models": await list_models(provider)}


@app.delete("/api/providers/{provider_id}", status_code=204)
async def delete_provider(provider_id: str) -> None:
    providers = load_providers(); filtered = [p for p in providers if p["id"] != provider_id]
    if len(filtered) == len(providers): raise HTTPException(404, "Provider not found")
    save_providers(filtered)


@app.get("/api/router-config")
async def get_router_config() -> dict[str, Any]: return load_router_config()


@app.put("/api/router-config")
async def update_router_config(data: RouterConfigInput) -> dict[str, Any]:
    save_router_config(data.model_dump()); return data.model_dump()


@app.get("/api/workspace/files")
async def workspace_files() -> dict[str, Any]: return {"workspace": str(WORKSPACE_DIR), "files": list_files()}


@app.get("/api/workspace/files/{path:path}")
async def workspace_read(path: str) -> dict[str, str]:
    try: return {"path": path, "content": read_file(path)}
    except (OSError, ValueError, UnicodeDecodeError) as exc: raise HTTPException(400, str(exc))


@app.put("/api/workspace/files/{path:path}", status_code=204)
async def workspace_write(path: str, data: FileWrite) -> None:
    try: write_file(path, data.content)
    except (OSError, ValueError) as exc: raise HTTPException(400, str(exc))


@app.post("/api/workspace/command")
async def workspace_command(data: CommandInput) -> dict[str, object]: return await run_command(data.command, data.timeout)


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
