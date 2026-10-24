"""Research mode support: gather numbered sources and check citations deterministically."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from backend.agents.tools.search import WebSearchUnavailable
from backend.core.logging import get_logger
from backend.decision.schemas import CitationsPayload
from backend.providers.base import ChatMessage, ProviderError
from backend.providers.router import CallMeta

if TYPE_CHECKING:
    from backend.agents.runtime import RunContext

log = get_logger(__name__)
_CITE_RE = re.compile(r"\[(\d{1,3})\]")


@dataclass
class Source:
    kind: str  # attachment | workspace | web
    title: str
    location: str
    excerpt: str

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "title": self.title, "location": self.location, "excerpt": self.excerpt}


async def gather_sources(rc: RunContext, query: str, attachments: list[dict[str, Any]],
                         web_k: int = 5, workspace_k: int = 5, use_native: bool = True
                         ) -> tuple[list[Source], str | None]:
    """Returns (sources, web_note). ``web_note`` explains why browsing was not possible, if it wasn't."""
    sources: list[Source] = []
    for att in attachments:
        if att.get("text"):
            sources.append(Source("attachment", att["filename"], f"attachment:{att['id']}", att["text"][:3000]))
    try:
        await rc.app.memory.index_workspace(rc.user_id, rc.workspace)
        for snippet in await rc.app.memory.search_workspace(rc.user_id, rc.workspace, query, k=workspace_k):
            location = snippet.source.removeprefix("file:").split("#", 1)[0]
            sources.append(Source("workspace", location, snippet.source, snippet.content[:2000]))
    except Exception:  # retrieval is best-effort
        log.warning("workspace retrieval failed", exc_info=True)
    web_note: str | None = None
    service = rc.app.web_search
    if use_native:
        try:
            response = await rc.app.router.chat(
                CallMeta(user_id=rc.user_id, run_id=rc.run_id, category="research_retrieval"),
                [ChatMessage(role="user", content=query)], model=rc.model_override or "auto",
                category="research_retrieval", require_capability="web_search", stream=False)
            for result in response.sources:
                sources.append(Source("web", result.get("title") or result.get("location", ""),
                                      result.get("location") or result.get("url", ""),
                                      result.get("excerpt", "")))
            if response.sources:
                return sources, None
            web_note = "The selected provider returned no web citations for this query."
        except ProviderError as exc:
            web_note = str(exc)
    if service.enabled and service.status()["configured"]:
        try:
            for result in await service.search(query, web_k):
                sources.append(Source("web", result.title or result.url, result.url, result.snippet))
            if web_note:
                web_note = "Using the configured Jevora Browser fallback because native provider web search " \
                           "was unavailable."
        except WebSearchUnavailable as exc:
            web_note = f"Web search failed: {exc}. The report uses attachments, workspace files and model knowledge."
    else:
        web_note = ("Web browsing is not available in this session, so this report relies on attached files, the "
                    "workspace and the model's own knowledge." if not web_note else
                    f"Web browsing is not available for the selected model: {web_note}")
    return sources, web_note


def render_sources(sources: list[Source]) -> str:
    if not sources:
        return "No sources were found; answer from general knowledge and say so."
    return "\n\n".join(f"[{i}] ({s.kind}) {s.title} — {s.location}\n{s.excerpt}" for i, s in enumerate(sources, 1))


def check_citations(report: str, sources: list[Source]) -> CitationsPayload:
    cited = {int(n) for n in _CITE_RE.findall(report)}
    valid = {n for n in cited if 1 <= n <= len(sources)}
    return CitationsPayload(sources_available=len(sources), sources_cited=len(valid),
                            unresolved_citations=len(cited - valid))
