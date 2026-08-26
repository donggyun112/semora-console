import pytest

from console.provider import DEFAULT_MODEL, openrouter_model
from console.store import make_store


def test_provider_raises_without_key(monkeypatch):
    for k in ("OPENROUTER_API_KEY", "OPENROUTER_KEY", "OPEN_ROTURE"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError):
        openrouter_model()


def test_provider_builds_with_key(monkeypatch):
    monkeypatch.delenv("MODEL", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    m = openrouter_model()
    assert DEFAULT_MODEL.startswith("deepseek/")
    assert m.model_name == DEFAULT_MODEL


@pytest.mark.asyncio
async def test_make_store_memory_without_db(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    store, closer = await make_store()
    assert store is not None and closer is None
