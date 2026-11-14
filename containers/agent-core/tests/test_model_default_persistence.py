from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent / "src"
_CORE_ROOT = _SRC.parent
if str(_CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(_CORE_ROOT))


class _Provider:
    def __init__(self, models: list[str]):
        self.models = models

    async def health_check(self) -> bool:
        return True

    def available_models(self) -> list[str]:
        return self.models


class _Redis:
    def __init__(self, values: dict[str, str], fail_set: bool = False):
        self.values = values
        self.fail_set = fail_set

    async def set(self, key: str, value: str) -> None:
        if self.fail_set:
            raise ConnectionError("Redis unavailable")
        self.values[key] = value

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def aclose(self) -> None:
        pass


def _manager():
    from src.models.manager import ModelManager

    manager = ModelManager()
    manager.providers = {"test-provider": _Provider(["model-a", "model-b"])}
    manager.fallback_order = ["test-provider"]
    manager.active_provider = "test-provider"
    manager.active_model = "model-a"
    manager.redis_url = "redis://test"
    return manager


@pytest.mark.asyncio
async def test_selected_model_is_restored_as_default(monkeypatch):
    stored: dict[str, str] = {}
    redis = _Redis(stored)
    monkeypatch.setattr("redis.asyncio.from_url", lambda *args, **kwargs: redis)

    manager = _manager()
    result = await manager.set_default_model("model-b")

    assert result == "Default model set to model-b"
    assert stored["model:default"] == "model-b"

    restarted = _manager()
    restarted.active_model = "model-a"
    await restarted.load_default_model()

    assert restarted.default_model == "model-b"
    assert restarted.active_model == "model-b"


@pytest.mark.asyncio
async def test_default_is_not_changed_when_persistence_fails(monkeypatch):
    redis = _Redis({}, fail_set=True)
    monkeypatch.setattr("redis.asyncio.from_url", lambda *args, **kwargs: redis)

    manager = _manager()
    result = await manager.set_default_model("model-b")

    assert result == "Could not persist default model"
    assert manager.active_model == "model-a"
    assert manager.default_model == ""
