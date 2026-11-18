"""Harness-agnostic external CLI execution engine for Agent WASP."""

from .base import (
    AgentEvent,
    EventType,
    HarnessAdapter,
    ModelOption,
    RunInput,
    RunResult,
    execute_harness,
    resolve_binary,
)
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .gemini import GeminiAdapter
from .pi import PiAdapter
from .registry import ADAPTER_NAMES, get_adapter, installed_harnesses, list_harness_models
from .runner import ActiveRunState, HarnessRunner, PendingHandoff
from .screens import ScreenClient

__all__ = [
    "ADAPTER_NAMES",
    "ActiveRunState",
    "AgentEvent",
    "ClaudeAdapter",
    "CodexAdapter",
    "EventType",
    "GeminiAdapter",
    "HarnessAdapter",
    "HarnessRunner",
    "ModelOption",
    "PendingHandoff",
    "PiAdapter",
    "RunInput",
    "RunResult",
    "ScreenClient",
    "execute_harness",
    "get_adapter",
    "installed_harnesses",
    "list_harness_models",
    "resolve_binary",
]
