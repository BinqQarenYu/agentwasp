"""Lightweight service registry for WASP agent runtime.

Provides a clean interface boundary between modules without tight coupling.
Each module registers itself once at startup; consumers look up by name or type.

Design:
- No external dependencies
- No circular imports (registry itself imports nothing from the agent)
- Replaceable: any registered service can be swapped at runtime
- Thread-safe for asyncio environments (GIL protects dict ops)

Usage:
    # At startup (main.py):
    registry.register("memory", memory_manager)
    registry.register("models", model_manager)

    # In any module:
    from ..runtime.registry import registry
    models = registry.get("models")
"""

from __future__ import annotations

import structlog
from typing import Any, TypeVar, Type

logger = structlog.get_logger()

T = TypeVar("T")


class ServiceRegistry:
    """Minimal service locator pattern for clean module interfaces.

    Not a full dependency injection framework — just a named lookup table.
    """

    def __init__(self, name: str = "default"):
        self._name = name
        self._services: dict[str, Any] = {}

    def register(self, name: str, service: Any) -> None:
        """Register a service under a name. Overwrites silently (for hot-swap)."""
        self._services[name] = service
        logger.debug("service_registry.registered", name=name, type=type(service).__name__)

    def get(self, name: str, default: Any = None) -> Any:
        """Look up a service by name. Returns default if not found."""
        return self._services.get(name, default)

    def require(self, name: str) -> Any:
        """Look up a service, raising if not found."""
        service = self._services.get(name)
        if service is None:
            raise RuntimeError(
                f"Service '{name}' not registered. "
                f"Available: {list(self._services.keys())}"
            )
        return service

    def get_typed(self, name: str, expected_type: Type[T]) -> T | None:
        """Type-safe lookup. Returns None if not found or wrong type."""
        service = self._services.get(name)
        if service is None:
            return None
        if not isinstance(service, expected_type):
            logger.warning(
                "service_registry.type_mismatch",
                name=name,
                expected=expected_type.__name__,
                got=type(service).__name__,
            )
            return None
        return service

    def unregister(self, name: str) -> bool:
        """Remove a service. Returns True if it existed."""
        if name in self._services:
            del self._services[name]
            logger.debug("service_registry.unregistered", name=name)
            return True
        return False

    def list_services(self) -> list[dict]:
        """List all registered services with their types."""
        return [
            {"name": name, "type": type(svc).__name__}
            for name, svc in self._services.items()
        ]

    def is_registered(self, name: str) -> bool:
        return name in self._services

    def __contains__(self, name: str) -> bool:
        return name in self._services

    def __repr__(self) -> str:
        return f"ServiceRegistry({self._name}, services={list(self._services.keys())})"


# Module-level singleton — populated in main.py
registry = ServiceRegistry("wasp")

# Convenience aliases for common service names
SERVICE_MEMORY = "memory"
SERVICE_MODELS = "models"
SERVICE_SKILLS = "skills"
SERVICE_EXECUTOR = "skill_executor"
SERVICE_SCHEDULER = "scheduler"
SERVICE_BUS = "bus"
SERVICE_HEALTH = "health_monitor"
SERVICE_INTROSPECTOR = "introspector"
SERVICE_BROKER = "broker"
SERVICE_METRICS = "metrics"
SERVICE_ECONOMICS = "economics"
