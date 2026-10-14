# Jevora IDE API

Version 0.2.0. Interactive docs: `/docs` (Swagger) and `/redoc` on the running server. Machine-readable spec: [openapi.json](openapi.json).

Authentication: when `AUTH_REQUIRED=true`, send `Authorization: Bearer <access_token>` (WebSockets: `?token=`). Tokens come from `/api/auth/login|signup|refresh`; the refresh token is an httpOnly cookie. With `AUTH_REQUIRED=false` (default) requests act as the local user.

## agents

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/agents` | List Agents |
| `POST` | `/api/agents` | Create Agent |
| `POST` | `/api/agents/generate` | Generate Agent |
| `GET` | `/api/agents/status` | Agents Status |
| `GET` | `/api/agents/templates` | Agent Templates |
| `DELETE` | `/api/agents/{agent_id}` | Delete Agent |
| `GET` | `/api/agents/{agent_id}` | Get Agent |
| `PUT` | `/api/agents/{agent_id}` | Update Agent |
| `POST` | `/api/agents/{agent_id}/run` | Run Agent |

## attachments

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/attachments` | List Attachments |
| `POST` | `/api/attachments` | Upload |
| `DELETE` | `/api/attachments/{attachment_id}` | Delete Attachment |
| `GET` | `/api/attachments/{attachment_id}` | Get Attachment |
| `GET` | `/api/attachments/{attachment_id}/content` | Attachment Content |

## auth

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/auth/config` | Auth Config |
| `POST` | `/api/auth/login` | Login |
| `POST` | `/api/auth/logout` | Logout |
| `GET` | `/api/auth/me` | Me |
| `GET` | `/api/auth/oauth/{provider}/callback` | Oauth Callback |
| `GET` | `/api/auth/oauth/{provider}/start` | Oauth Start |
| `POST` | `/api/auth/refresh` | Refresh |
| `POST` | `/api/auth/signup` | Signup |

## extras

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/commands` | Commands |
| `GET` | `/api/decision/config` | Get Decision Config |
| `PUT` | `/api/decision/config` | Put Decision Config |
| `POST` | `/api/decision/eval` | Run Decision Eval — Evaluate engines on seed + labelled decisions; optionally apply Laya's promotion gate. |
| `GET` | `/api/decision/eval/latest` | Latest Decision Eval |
| `GET` | `/api/decisions` | List Decisions |
| `POST` | `/api/decisions/{decision_id}/label` | Label Decision |
| `GET` | `/api/plugins` | List Plugins |
| `PATCH` | `/api/plugins/{plugin_id}` | Patch Plugin |

## mcp

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/mcp/servers` | List Servers |
| `POST` | `/api/mcp/servers` | Create Server |
| `DELETE` | `/api/mcp/servers/{server_id}` | Delete Server |
| `PATCH` | `/api/mcp/servers/{server_id}` | Patch Server |
| `GET` | `/api/mcp/servers/{server_id}/health` | Server Health |
| `GET` | `/api/mcp/servers/{server_id}/tools` | Server Tools |
| `POST` | `/api/mcp/servers/{server_id}/tools/{tool_name}/call` | Call Tool |

## providers

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/models` | Model Catalogue |
| `GET` | `/api/providers` | List Providers |
| `POST` | `/api/providers` | Create Provider |
| `DELETE` | `/api/providers/{provider_id}` | Delete Provider |
| `PUT` | `/api/providers/{provider_id}` | Update Provider |
| `GET` | `/api/providers/{provider_id}/models` | Provider Models |
| `GET` | `/api/router/config` | Get Router Config |
| `PUT` | `/api/router/config` | Put Router Config |

## runs

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/runs` | List Runs |
| `POST` | `/api/runs` | Create Run |
| `DELETE` | `/api/runs/{run_id}` | Delete Run |
| `GET` | `/api/runs/{run_id}` | Get Run |
| `POST` | `/api/runs/{run_id}/cancel` | Cancel Run |
| `GET` | `/api/runs/{run_id}/diff` | Run Diff |
| `POST` | `/api/runs/{run_id}/escalation` | Answer Escalation |
| `GET` | `/api/runs/{run_id}/events` | Run Events Sse |
| `POST` | `/api/runs/{run_id}/resume` | Resume Run |
| `GET` | `/api/runs/{run_id}/tree` | Run Tree |

## system

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/features` | Features |
| `GET` | `/api/system/health` | System Health |
| `GET` | `/api/usage` | Usage |
| `GET` | `/metrics` | Metrics |

## vault

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/vault/config` | Get Config |
| `PUT` | `/api/vault/config` | Put Config |
| `POST` | `/api/vault/reindex` | Reindex |
| `GET` | `/api/vault/runs` | Vault Runs |
| `GET` | `/api/vault/runs/{run_id}` | Vault Run |

## vms

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/vms` | List Vms |
| `POST` | `/api/vms` | Create Vm |
| `DELETE` | `/api/vms/{vm_id}` | Delete Vm |
| `POST` | `/api/vms/{vm_id}/select` | Select Vm |
| `POST` | `/api/vms/{vm_id}/start` | Start Vm |
| `POST` | `/api/vms/{vm_id}/stop` | Stop Vm |

## workspace

| Method | Path | Summary |
|---|---|---|
| `GET` | `/api/workspace` | Workspace Info |
| `GET` | `/api/workspace/changes` | Changes |
| `DELETE` | `/api/workspace/file` | Delete File |
| `GET` | `/api/workspace/file` | Read File |
| `PUT` | `/api/workspace/file` | Write File |
| `GET` | `/api/workspace/tree` | Tree |


## WebSockets & streaming

| Endpoint | Purpose |
|---|---|
| `WS /api/runs/{id}/stream?last_seq=N&token=JWT` | Typed events for one run. Reconnect with the last `seq` you saw to replay missed events. A fresh connection starts with a `run.status` snapshot. |
| `GET /api/runs/{id}/events?last_seq=N` (SSE, also honours `Last-Event-ID`) | Same events as Server-Sent Events; closes after a terminal status. |
| `WS /api/events?last_seq=N&token=JWT` | Global per-user feed (`run.*`, `agents.status`, `file.changed`, …; `token.delta` excluded). |
| `WS /api/terminal?cols=&rows=&vm_id=&token=JWT` | PTY in the sandbox. Client → `{"type":"stdin","data"}`, `{"type":"resize","cols","rows"}`; server → `ready`, `stdout`, `exit`, `error`. |

Every event: `{"type", "seq", "ts", "run_id", "call_id", "parent_call_id", "data"}` with types
`run.status`, `run.created`, `agent.started|finished|failed`, `subagent.spawned`, `token.delta`, `token.reset`,
`tool.call|result`, `file.changed`, `terminal.output`, `test.result`, `artifact.created`, `decision.made`,
`escalation.requested|resolved`, `vault.written`, `agents.status`, `plan.created`, `error`.

Terminal run statuses (what `app.js` polls for): `done`, `failed`, `escalated`. Non-terminal: `queued`, `planning`,
`running`, `verifying`, `awaiting_user`.

