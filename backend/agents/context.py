"""Non-destructive, provider-neutral projection of persisted conversation history.

Estimate ceil(UTF-8 bytes / 4) plus 12 tokens per message. This approximates the
usual chars/4 heuristic, counts non-ASCII text more conservatively, and includes
serialized tool arguments/schemas. Images reserve 4096 tokens apiece. These are
estimates, not a tokenizer guarantee; leave another 4096 tokens for output/tool
overhead. Context capacity is independent of cumulative run/agent usage limits.
No provider-wide cap is guessed: local servers may support arbitrary windows.
"""

from __future__ import annotations

import json

from backend.providers.base import ChatMessage, ToolSpec

CONTEXT_RESERVE_TOKENS = 4096


def estimate_tokens(text: str) -> int:
    return (len(text.encode("utf-8")) + 3) // 4


def message_tokens(message: ChatMessage) -> int:
    tools = json.dumps([t.model_dump() for t in message.tool_calls], ensure_ascii=False) if message.tool_calls else ""
    images = sum(p.type == "image" for p in message.content) if isinstance(message.content, list) else 0
    return 12 + estimate_tokens(message.text() + tools) + images * 4096


def project_history(history: list[ChatMessage], current: list[ChatMessage], tools: list[ToolSpec],
                    context_window_tokens: int) -> list[ChatMessage]:
    """Keep the newest contiguous suffix of whole messages that fits the remaining window.

    The assembled system prompt, current request (including attachments), live tool
    dialogue and tool schemas take priority. Oversized messages are never truncated
    or skipped to pull in older context. The database is never modified.
    """
    reserved = CONTEXT_RESERVE_TOKENS + sum(message_tokens(m) for m in current)
    reserved += estimate_tokens(json.dumps([t.model_dump() for t in tools], ensure_ascii=False)) if tools else 0
    remaining = max(0, context_window_tokens - reserved)
    selected: list[ChatMessage] = []
    for message in reversed(history):
        cost = message_tokens(message)
        if cost > remaining:
            break
        selected.append(message)
        remaining -= cost
    return list(reversed(selected))
