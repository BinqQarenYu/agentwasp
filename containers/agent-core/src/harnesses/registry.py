"""Registry and cached model discovery for external CLI harnesses."""

from __future__ import annotations

import threading
import time
from typing import Callable

from .base import HarnessAdapter, ModelOption, resolve_binary
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .gemini import GeminiAdapter
from .pi import PiAdapter

ADAPTER_NAMES: tuple[str, ...] = ("claude", "codex", "pi", "gemini")

_FACTORIES: dict[str, Callable[[], HarnessAdapter]] = {
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "pi": PiAdapter,
    "gemini": GeminiAdapter,
}

_MODEL_CACHE_TTL_SECONDS = 600.0
_model_cache_lock = threading.Lock()
_model_cache: dict[str, tuple[float, list[ModelOption]]] = {}


def get_adapter(name: str) -> HarnessAdapter | None:
    """Instantiate a fresh adapter instance for the given harness name."""
    factory = _FACTORIES.get((name or "").strip().lower())
    return factory() if factory else None


def installed_harnesses() -> list[str]:
    """Return all registered harnesses whose CLI binary is available on PATH."""
    found: list[str] = []
    for name in ADAPTER_NAMES:
        adapter = get_adapter(name)
        if adapter and resolve_binary(adapter.bin_name):
            found.append(name)
    return found


def list_harness_models(name: str, force_refresh: bool = False) -> list[ModelOption]:
    """Return available models for a harness, cached for 10 minutes."""
    key = (name or "").strip().lower()
    now = time.monotonic()
    if not force_refresh:
        with _model_cache_lock:
            cached = _model_cache.get(key)
            if cached and (now - cached[0]) < _MODEL_CACHE_TTL_SECONDS and cached[1]:
                return list(cached[1])

    adapter = get_adapter(key)
    if not adapter:
        return []
    models = adapter.list_models()
    with _model_cache_lock:
        _model_cache[key] = (now, list(models))
    return models
