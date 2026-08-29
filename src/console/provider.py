"""OpenRouter chat model. Recovers DeepSeek DSML tool markup OpenRouter leaves as text."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessageChunk
from langchain_openai import ChatOpenAI
from nexora.engines.plain.dsml import DsmlFilter, parse_dsml_tool_calls
from pydantic import SecretStr

DEFAULT_MODEL = "~deepseek/deepseek-v4-flash-latest"


def model_name() -> str:
    """The model id every caller reports and runs. MODEL wins, empty falls back."""
    return os.getenv("MODEL") or DEFAULT_MODEL


def _chunk_text(chunk: Any) -> str:
    text = getattr(chunk, "text", None)
    if isinstance(text, str) and text:
        return text
    content = getattr(chunk, "content", "")
    return content if isinstance(content, str) else ""


def _has_native_calls(chunk: Any) -> bool:
    return bool(getattr(chunk, "tool_calls", None) or getattr(chunk, "tool_call_chunks", None))


async def recover_dsml_chunks(
    chunks: AsyncIterator[AIMessageChunk],
) -> AsyncIterator[AIMessageChunk]:
    """Suppress leaked DSML text, including truncated open tags, and emit tool_calls."""
    pending: list[AIMessageChunk] = []
    filt = DsmlFilter()
    last: AIMessageChunk | None = None
    native = False
    async for chunk in chunks:
        last = chunk
        if _has_native_calls(chunk):
            # The held buffer is ordinary text once a native call arrives — dropping it
            # silently ate the tail of the sentence before the call.
            held = filt.finish()
            if held:
                yield AIMessageChunk(content=held)
            pending.clear()
            native = True
            yield chunk
            continue
        piece = _chunk_text(chunk)
        visible = filt.push(piece)
        if visible:
            # Visible output already carries whatever was buffered before it. Leaving
            # those chunks in `pending` replayed them again after the whole reply.
            pending.clear()
            yield chunk.model_copy(update={"content": visible}) if piece != visible else chunk
        elif piece:
            pending.append(chunk)
    leftover = filt.finish()
    if leftover:
        yield AIMessageChunk(content=leftover)
        return
    if native:
        return
    calls = parse_dsml_tool_calls(filt.markup)
    if not calls:
        for held in pending:
            yield held
        return
    yield AIMessageChunk(
        content="",
        tool_calls=calls,
        id=getattr(last, "id", None),
        response_metadata=getattr(last, "response_metadata", {}) or {},
    )


class _DsmlRecovering:
    """bind_tools-preserving wrapper that recovers DSML left in streamed content."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def bind_tools(self, *args: Any, **kwargs: Any) -> _DsmlRecovering:
        return _DsmlRecovering(self._inner.bind_tools(*args, **kwargs))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def astream(self, *args: Any, **kwargs: Any) -> AsyncIterator[AIMessageChunk]:
        async for chunk in recover_dsml_chunks(self._inner.astream(*args, **kwargs)):
            yield chunk


def openrouter_model(name: str | None = None) -> Any:
    """Create a streaming OpenRouter chat model from environment configuration."""
    # OPEN_ROTURE is the (misspelled) fixture var used across this workspace's .env files.
    key = (
        os.getenv("OPENROUTER_API_KEY")
        or os.getenv("OPENROUTER_KEY")
        or os.getenv("OPEN_ROTURE")
        or ""
    )
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    return _DsmlRecovering(
        ChatOpenAI(
            model=name or model_name(),
            api_key=SecretStr(key),
            base_url="https://openrouter.ai/api/v1",
            default_headers={"X-Title": "Nexora Control Plane Console"},
            max_retries=1,
            streaming=True,
            timeout=60,
        )
    )
