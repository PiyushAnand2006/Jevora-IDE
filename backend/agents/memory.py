"""Agent memory scopes.

* ``run``       — scratchpad only; lives in the orchestrator state for one run.
* ``agent``     — notes persisted across runs for that agent (``agent_memories``).
* ``workspace`` — shared project knowledge: workspace text chunks + run summaries, retrieved by
                  embedding similarity when an embedding model is configured, else BM25 (lexical).
* ``none``      — nothing is recalled or stored.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from backend.agents.workspace_state import iter_files
from backend.core.logging import get_logger
from backend.core.paths import is_binary
from backend.db.models import AgentMemory, Workspace, WorkspaceMemory

if TYPE_CHECKING:
    from backend.app_context import AppContext

log = get_logger(__name__)
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]+|\d+")
CHUNK_CHARS = 1800
MAX_INDEX_FILES = 400
TEXT_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".jsx", ".md", ".txt", ".json", ".toml", ".yaml", ".yml", ".html",
                 ".css", ".go", ".rs", ".java", ".rb", ".php", ".sh", ".sql", ".cfg", ".ini", ".vue", ".svelte"}


_CAMEL_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")


def tokenize(text: str) -> list[str]:
    """Words plus identifier parts: ``compute_invoice_total`` → compute_invoice_total, compute, invoice, total."""
    out: list[str] = []
    for token in _TOKEN_RE.findall(text):
        lower = token.lower()
        out.append(lower)
        parts = [p.lower() for chunk in token.split("_") for p in _CAMEL_RE.findall(chunk)]
        if len(parts) > 1:
            out.extend(parts)
    return out


@dataclass
class Snippet:
    source: str
    content: str
    score: float


def bm25_rank(query: str, docs: list[tuple[str, str]], k: int = 6) -> list[Snippet]:
    q = tokenize(query)
    if not q or not docs:
        return []
    tokenized = [tokenize(content) for _, content in docs]
    avgdl = sum(len(t) for t in tokenized) / max(1, len(tokenized))
    df: Counter[str] = Counter()
    for toks in tokenized:
        df.update(set(toks))
    n = len(docs)
    scored: list[Snippet] = []
    for (source, content), toks in zip(docs, tokenized, strict=True):
        tf = Counter(toks)
        score = 0.0
        for term in q:
            if term not in tf:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            denom = tf[term] + 1.5 * (1 - 0.75 + 0.75 * len(toks) / max(1.0, avgdl))
            score += idf * tf[term] * 2.5 / denom
        if score > 0:
            scored.append(Snippet(source, content, round(score, 4)))
    scored.sort(key=lambda s: s.score, reverse=True)
    return scored[:k]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def chunk_text(text: str, size: int = CHUNK_CHARS) -> list[str]:
    lines = text.splitlines(keepends=True)
    chunks: list[str] = []
    current = ""
    for line in lines:
        if len(current) + len(line) > size and current:
            chunks.append(current)
            current = ""
        current += line
    if current.strip():
        chunks.append(current)
    return chunks


class MemoryService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._index_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ agent scope
    async def agent_notes(self, user_id: str, agent_key: str, limit: int = 6) -> list[str]:
        async with self.ctx.sessions() as session:
            rows = await session.scalars(select(AgentMemory).where(
                AgentMemory.user_id == user_id, AgentMemory.agent_key == agent_key)
                .order_by(AgentMemory.created_at.desc()).limit(limit))
            return [r.content for r in rows][::-1]

    async def remember_agent(self, user_id: str, agent_key: str, run_id: str | None, content: str) -> None:
        if not content.strip():
            return
        async with self.ctx.sessions() as session:
            session.add(AgentMemory(user_id=user_id, agent_key=agent_key, run_id=run_id, content=content[:4000]))
            await session.commit()

    # ------------------------------------------------------------------ workspace scope
    async def workspace_id(self, user_id: str, root: Path) -> str:
        async with self.ctx.sessions() as session:
            ws = await session.scalar(select(Workspace).where(Workspace.user_id == user_id,
                                                              Workspace.root_path == str(root)))
            if ws is None:
                ws = Workspace(user_id=user_id, name=root.name or "workspace", root_path=str(root), is_default=True)
                session.add(ws)
                await session.commit()
                await session.refresh(ws)
            return ws.id

    async def index_workspace(self, user_id: str, root: Path) -> int:
        """Incrementally (re)index workspace text files; returns the number of chunks written."""
        ws_id = await self.workspace_id(user_id, root)
        lock = self._index_locks.setdefault(ws_id, asyncio.Lock())
        async with lock:
            files = await asyncio.to_thread(self._collect, root)
            async with self.ctx.sessions() as session:
                existing = {(m.source, hashlib.sha1(m.content.encode(), usedforsecurity=False).hexdigest())
                            for m in await session.scalars(select(WorkspaceMemory).where(
                                WorkspaceMemory.workspace_id == ws_id, WorkspaceMemory.source.like("file:%")))}
            sources_now = {f"file:{rel}" for rel, _ in files}
            new_chunks: list[tuple[str, str]] = []
            changed_sources: set[str] = set()
            for rel, text in files:
                for index, chunk in enumerate(chunk_text(text)):
                    source = f"file:{rel}#{index}"
                    digest = hashlib.sha1(chunk.encode(), usedforsecurity=False).hexdigest()
                    if (source, digest) not in existing:
                        new_chunks.append((source, chunk))
                        changed_sources.add(f"file:{rel}")
            vectors = await self.ctx.router.embed(user_id, [c for _, c in new_chunks]) if new_chunks else None
            async with self.ctx.sessions() as session:
                stale = [s for s in changed_sources]
                for source in stale:
                    await session.execute(delete(WorkspaceMemory).where(
                        WorkspaceMemory.workspace_id == ws_id, WorkspaceMemory.source.like(f"{source}#%")))
                gone = await session.scalars(select(WorkspaceMemory.source).where(
                    WorkspaceMemory.workspace_id == ws_id, WorkspaceMemory.source.like("file:%")))
                for source in set(gone):
                    if source.split("#", 1)[0] not in sources_now:
                        await session.execute(delete(WorkspaceMemory).where(
                            WorkspaceMemory.workspace_id == ws_id, WorkspaceMemory.source == source))
                for i, (source, chunk) in enumerate(new_chunks):
                    vector = vectors[i] if vectors and i < len(vectors) else None
                    session.add(WorkspaceMemory(workspace_id=ws_id, source=source, content=chunk, embedding=vector))
                await session.commit()
            return len(new_chunks)

    @staticmethod
    def _collect(root: Path) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for rel, st in iter_files(root):
            if Path(rel).suffix.lower() not in TEXT_SUFFIXES or st.st_size > 200_000:
                continue
            try:
                data = (root / rel).read_bytes()
            except OSError:
                continue
            if is_binary(data[:8192]):
                continue
            out.append((rel, data.decode("utf-8", "replace")))
            if len(out) >= MAX_INDEX_FILES:
                break
        return out

    async def remember_workspace(self, user_id: str, root: Path, source: str, content: str) -> None:
        ws_id = await self.workspace_id(user_id, root)
        vectors = await self.ctx.router.embed(user_id, [content[:4000]])
        async with self.ctx.sessions() as session:
            session.add(WorkspaceMemory(workspace_id=ws_id, source=source, content=content[:4000],
                                        embedding=vectors[0] if vectors else None))
            await session.commit()

    async def search_workspace(self, user_id: str, root: Path, query: str, k: int = 6) -> list[Snippet]:
        ws_id = await self.workspace_id(user_id, root)
        async with self.ctx.sessions() as session:
            rows = list(await session.scalars(select(WorkspaceMemory).where(WorkspaceMemory.workspace_id == ws_id)))
        if not rows:
            return []
        lexical = bm25_rank(query, [(r.source, r.content) for r in rows], k=max(k * 4, 20))
        with_vectors = [r for r in rows if r.embedding]
        query_vec = await self.ctx.router.embed(user_id, [query]) if with_vectors else None
        if not query_vec:
            return lexical[:k]
        semantic = sorted((Snippet(r.source, r.content, round(cosine(query_vec[0], r.embedding or []), 4))
                           for r in with_vectors), key=lambda s: s.score, reverse=True)[: max(k * 4, 20)]
        semantic = [s for s in semantic if s.score > 0.2]
        # Hybrid retrieval: reciprocal-rank fusion of lexical (exact identifiers) and semantic hits.
        fused: dict[str, tuple[float, Snippet]] = {}
        for ranking in (lexical, semantic):
            for rank, snippet in enumerate(ranking):
                score = 1.0 / (60 + rank)
                previous = fused.get(snippet.source)
                fused[snippet.source] = ((previous[0] if previous else 0.0) + score, snippet)
        ordered = sorted(fused.values(), key=lambda item: item[0], reverse=True)[:k]
        return [Snippet(s.source, s.content, round(score, 5)) for score, s in ordered]
