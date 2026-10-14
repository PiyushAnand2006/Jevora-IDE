"""ui_designer node: the creative-UI worker (creative_ui model category)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from backend.orchestrator.nodes.coder import build_worker

if TYPE_CHECKING:
    from backend.orchestrator.graph import Orchestrator
    from backend.orchestrator.state import GraphState


async def ui_designer(o: Orchestrator, state: GraphState) -> dict[str, Any]:
    return await build_worker(o, state, "ui-designer")
