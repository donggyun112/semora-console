"""OpenRouter chat model.

Pydantic AI owns the model client and its native tool protocol.
"""

from __future__ import annotations

import os

from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openrouter import OpenRouterProvider

DEFAULT_MODEL = "~deepseek/deepseek-v4-flash-latest"


def model_name() -> str:
    """The model id every caller reports and runs. MODEL wins, empty falls back."""
    return os.getenv("MODEL") or DEFAULT_MODEL


def openrouter_model(name: str | None = None) -> OpenAIChatModel:
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
    return OpenAIChatModel(
        name or model_name(),
        provider=OpenRouterProvider(api_key=key),
    )
