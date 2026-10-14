# Plugins

Jevora ships three built-in plugins whose toggles change orchestrator behaviour (persisted per user via
`PATCH /api/plugins/{id}`):

| id | When off |
|---|---|
| `decision_verification` | No verify → retry loop: work goes from tester/critic straight to finalize, and only a completion verdict is logged. |
| `iterative_tester` | No independent test pass (and no auto-written tests) after coding. Verification uses whatever test evidence the coder produced itself. |
| `dynamic_model_router` | Every agent uses one default binding instead of per-category tiers. |

## Third-party plugins

A plugin is a Python module with a `register(api)` function. It is discovered in one of two ways:

1. **Entry point** — in your package's `pyproject.toml`:
   ```toml
   [project.entry-points."jevora.plugins"]
   my_plugin = "my_package.jevora_plugin"
   ```
2. **Manifest** — a folder in `PLUGINS_DIR` (e.g. `./plugins/my_plugin/`) containing `plugin.json`:
   ```json
   {"id": "my_plugin", "name": "My Plugin", "description": "…", "version": "1.0.0",
    "module": "plugin.py", "default_enabled": false}
   ```

`register(api)` can:

```python
from backend.agents.tools.base import Tool, ToolContext, ToolResult, schema

class HelloTool(Tool):
    name = "hello"
    description = "Say hello."
    permission = "fs.read"          # agents only see tools whose permission they hold
    parameters = schema({"name": {"type": "string"}}, ["name"])

    async def run(self, ctx: ToolContext, args: dict) -> ToolResult:
        return ToolResult(content=f"hello, {args['name']}!")

def register(api):
    api.add_tool(HelloTool())                         # tools
    api.add_hook("before_run", on_before_run)         # run hooks: before_run(run_id, resumed), after_run(run_id)
    api.add_hook("before_node", on_before_node)       # graph hooks: before_node(node, run_id, state)
    api.add_hook("after_node", on_after_node)         #              after_node(node, run_id, updates)
    api.add_hook("on_decision", on_decision)          # every acted decision: on_decision(decision, run_id)
```

Hooks are async callables that receive keyword arguments. They only run for users who have the plugin
enabled, and an exception in a hook is logged without affecting the run. Tools a plugin produces should
create evidence only through `EvidenceFactory` if they want the Decision Engine to judge it.

A working example lives in `tests/fixtures/plugins/hello_plugin/`.
