"""Optional Jevora Browser fallback (Tavily, Brave or SearXNG) and its tool.

Native provider web search is handled by the provider adapters; this service is
kept for custom/local models and deployments that explicitly configure a
server-side search backend.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel

from backend.agents.tools.base import Tool, ToolContext, ToolResult, schema
from backend.config import Settings
from backend.core.logging import get_logger

log = get_logger(__name__)


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str = ""


class WebSearchUnavailable(Exception):
    pass


class WebSearchService:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    @property
    def enabled(self) -> bool:
        return self.settings.web_search_enabled

    def status(self) -> dict[str, Any]:
        s = self.settings
        configured = {"tavily": bool(s.tavily_api_key), "brave": bool(s.brave_api_key),
                      "searxng": bool(s.searxng_url)}.get(s.web_search_provider, False)
        return {"enabled": s.web_search_enabled, "provider": s.web_search_provider, "configured": configured}

    async def search(self, query: str, k: int = 5) -> list[SearchResult]:
        if not self.enabled:
            raise WebSearchUnavailable("web search is disabled (WEB_SEARCH_ENABLED=false)")
        provider = self.settings.web_search_provider
        try:
            if provider == "tavily":
                return await self._tavily(query, k)
            if provider == "brave":
                return await self._brave(query, k)
            return await self._searxng(query, k)
        except httpx.HTTPError as exc:
            raise WebSearchUnavailable(f"{provider} search failed: {type(exc).__name__}") from exc

    async def _tavily(self, query: str, k: int) -> list[SearchResult]:
        key = self.settings.tavily_api_key
        if key is None:
            raise WebSearchUnavailable("TAVILY_API_KEY is not set")
        response = await self.client.post("https://api.tavily.com/search", timeout=20, json={
            "api_key": key.get_secret_value(), "query": query, "max_results": k, "search_depth": "basic"})
        response.raise_for_status()
        return [SearchResult(title=r.get("title", ""), url=r.get("url", ""), snippet=r.get("content", "")[:800])
                for r in response.json().get("results", [])][:k]

    async def _brave(self, query: str, k: int) -> list[SearchResult]:
        key = self.settings.brave_api_key
        if key is None:
            raise WebSearchUnavailable("BRAVE_API_KEY is not set")
        response = await self.client.get("https://api.search.brave.com/res/v1/web/search", timeout=20,
                                         params={"q": query, "count": k},
                                         headers={"X-Subscription-Token": key.get_secret_value(),
                                                  "Accept": "application/json"})
        response.raise_for_status()
        items = (response.json().get("web") or {}).get("results", [])
        return [SearchResult(title=r.get("title", ""), url=r.get("url", ""), snippet=r.get("description", "")[:800])
                for r in items][:k]

    async def _searxng(self, query: str, k: int) -> list[SearchResult]:
        base = self.settings.searxng_url
        if not base:
            raise WebSearchUnavailable("SEARXNG_URL is not set")
        response = await self.client.get(f"{base.rstrip('/')}/search", timeout=20,
                                         params={"q": query, "format": "json"})
        response.raise_for_status()
        return [SearchResult(title=r.get("title", ""), url=r.get("url", ""), snippet=r.get("content", "")[:800])
                for r in response.json().get("results", [])][:k]


class WebSearchTool(Tool):
    name = "web_search"
    description = "Search the web (only when enabled by the administrator). Returns titles, URLs and snippets."
    permission = "web.search"
    parameters = schema({"query": {"type": "string"}, "max_results": {"type": "integer", "default": 5}}, ["query"])

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        service: WebSearchService = ctx.app.web_search
        try:
            results = await service.search(str(args["query"]), max(1, min(int(args.get("max_results") or 5), 10)))
        except WebSearchUnavailable as exc:
            return ToolResult(ok=False, content=f"web search unavailable: {exc}")
        lines = [f"[{i}] {r.title}\n{r.url}\n{r.snippet}" for i, r in enumerate(results, 1)]
        return ToolResult(content="\n\n".join(lines) or "no results",
                          data={"results": [r.model_dump() for r in results]})
