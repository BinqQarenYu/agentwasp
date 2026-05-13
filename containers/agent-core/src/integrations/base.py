"""Integration base types — BaseConnector, ConnectorManifest, ActionSpec, RiskLevel.

All connectors must implement BaseConnector.  Manifests are validated at
registration time.  Secrets flow through SecretVault and are NEVER exposed
to the LLM.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Risk classification
# ---------------------------------------------------------------------------

class RiskLevel(str, Enum):
    LOW      = "low"       # Read-only, reversible, no external mutations
    MEDIUM   = "medium"    # Writes to external service, reversible
    HIGH     = "high"      # Irreversible external mutations (send message, delete)
    CRITICAL = "critical"  # Always blocked — destructive or privileged ops


# ---------------------------------------------------------------------------
# Manifest primitives
# ---------------------------------------------------------------------------

@dataclass
class ParamSpec:
    name: str
    type: str           # "string" | "integer" | "boolean" | "object" | "array"
    description: str
    required: bool = True
    default: Any = None


@dataclass
class ActionSpec:
    id: str
    description: str
    params: list[ParamSpec]
    risk_level: RiskLevel
    capability: str     # Mirrors CapabilityLevel intent: "monitored" | "controlled" | "restricted"


@dataclass
class RateLimit:
    requests_per_minute: int = 60
    requests_per_hour: int   = 1_000
    requests_per_day: int    = 10_000


@dataclass
class SecretSpec:
    """Describes a single secret a connector needs.

    The dashboard renders `label` instead of the raw key, shows `help` below the
    field, and uses `placeholder`/`example` to give the user a concrete shape to
    aim at (e.g. "123456789:ABCdef..." vs an empty box that says nothing).
    """
    key: str                           # Stored vault key — e.g. "bot_token"
    label: str                         # Human label — e.g. "Bot token"
    help: str = ""                     # One-line guidance shown under the input
    placeholder: str = ""              # Greys-out text in empty input
    example: str = ""                  # Example value shown beneath the input
    required: bool = True              # If False, render as optional


@dataclass
class ConnectorManifest:
    id: str                          # Unique, kebab-case: "discord", "github"
    version: str                     # Semver: "1.0.0"
    name: str                        # Display name: "Discord"
    category: str                    # "chat" | "productivity" | "smart_home" | "tools" | "ai_model" | "media" | "social"
    description: str
    capabilities: list[str]          # Human-readable capability list
    risk_level: RiskLevel            # Highest risk level across all actions
    actions: list[ActionSpec]
    required_secrets: list[str]      # Secret keys that must be in vault before execution
    config_schema: dict[str, Any]    # JSON-schema for optional non-secret config
    rate_limits: dict[str, RateLimit]  # action_id → RateLimit
    homepage: str = ""
    docs_url: str = ""
    # Optional rich secret schema. When present, dashboard uses these instead of
    # bare `required_secrets` keys to render friendly forms. Older connectors
    # that only set `required_secrets` continue to work (UI falls back to key).
    secret_specs: list[SecretSpec] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class IntegrationError(Exception):
    """Base exception for all integration errors."""


class CircuitBreakerOpenError(IntegrationError):
    def __init__(self, integration_id: str) -> None:
        super().__init__(f"Circuit breaker OPEN for '{integration_id}' — too many failures")
        self.integration_id = integration_id


class PolicyDeniedError(IntegrationError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"Policy denied: {reason}")
        self.reason = reason


class SecretMissingError(IntegrationError):
    def __init__(self, integration_id: str, secret_key: str) -> None:
        super().__init__(
            f"Required secret '{secret_key}' not configured for integration '{integration_id}'"
        )
        self.integration_id = integration_id
        self.secret_key = secret_key


class ActionNotFoundError(IntegrationError):
    def __init__(self, action: str, integration_id: str = "") -> None:
        ctx = f" in '{integration_id}'" if integration_id else ""
        super().__init__(f"Action '{action}' not found{ctx}")


class IntegrationNotFoundError(IntegrationError):
    def __init__(self, integration_id: str) -> None:
        super().__init__(f"Integration '{integration_id}' is not registered")
        self.integration_id = integration_id


# ---------------------------------------------------------------------------
# Abstract base connector
# ---------------------------------------------------------------------------

class BaseConnector(ABC):
    """Abstract base that every WASP connector must implement.

    Lifecycle:
        1. Connector is instantiated (no I/O in __init__)
        2. Registered in IntegrationRegistry
        3. health_check() called periodically
        4. execute() called via registry (secrets injected, never from LLM)

    Secret contract:
        - Connectors NEVER store secrets as instance attributes after __init__
        - Secrets are received in execute() → used → discarded
        - This prevents accidental logging / serialisation of secrets
    """

    @abstractmethod
    def manifest(self) -> ConnectorManifest:
        """Return the immutable connector manifest."""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Return True if the integration endpoint is reachable and functional."""
        ...

    @abstractmethod
    async def execute(
        self,
        action: str,
        params: dict[str, Any],
        secrets: dict[str, str],
    ) -> dict[str, Any]:
        """Execute action with resolved params and secrets.

        Args:
            action:  Action ID from manifest.actions
            params:  Validated, LLM-supplied parameters (no secrets here)
            secrets: Resolved from SecretVault — NEVER passed back to LLM

        Returns:
            {"ok": bool, "data": Any, "error": str | None}
        """
        ...

    def get_metrics(self) -> dict[str, Any]:
        """Return current metrics snapshot for dashboard display."""
        return {}

    def get_status(self) -> dict[str, Any]:
        """Return human-readable connection status."""
        return {"status": "unknown"}

    # ------------------------------------------------------------------
    # Helpers available to subclasses
    # ------------------------------------------------------------------

    @staticmethod
    def ok(data: Any = None) -> dict[str, Any]:
        return {"ok": True, "data": data, "error": None}

    @staticmethod
    def err(message: str) -> dict[str, Any]:
        return {"ok": False, "data": None, "error": message}
