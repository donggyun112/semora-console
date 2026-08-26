"""OpenRouter chat model. Plain ChatOpenAI pointed at OpenRouter — this demo does not
need the reasoning-preserving subclass."""

from __future__ import annotations

import os

from langchain_openai import ChatOpenAI
from pydantic import SecretStr

DEFAULT_MODEL = "deepseek/deepseek-v4-flash-0731"


def openrouter_model(name: str | None = None) -> ChatOpenAI:
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
    return ChatOpenAI(
        model=name or os.getenv("MODEL", DEFAULT_MODEL),
        api_key=SecretStr(key),
        base_url="https://openrouter.ai/api/v1",
        default_headers={"X-Title": "Nexora Control Plane Console"},
        max_retries=1,
        streaming=True,
        timeout=60,
    )
