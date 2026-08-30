"""OpenRouter chat model.

The DSML repair this file used to carry now lives in ``semora_llm``, where the provider
client belongs. One implementation, and the console stops shipping its own.
"""

from __future__ import annotations

import os

from semora_llm import ChatModel, openrouter

DEFAULT_MODEL = "~deepseek/deepseek-v4-flash-latest"


def model_name() -> str:
    """The model id every caller reports and runs. MODEL wins, empty falls back."""
    return os.getenv("MODEL") or DEFAULT_MODEL


def openrouter_model(name: str | None = None) -> ChatModel:
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
    return openrouter(
        name or model_name(),
        api_key=key,
        title="Semora Control Plane Console",
        # A public link should not let one hung request hold a worker for the SDK's
        # ten-minute default.
        timeout=60,
    )
