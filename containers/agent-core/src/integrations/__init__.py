"""WASP Integration Expansion Platform.

Entry points:
    from .registry import IntegrationRegistry
    from .vault import SecretVault
    from .policy import PolicyEngine
    from .skill_bridge import IntegrationSkillBridge

Connectors are registered in main.py via registry.register(Connector()).
The skill bridge is registered in skill_registry as "integration".
"""

from .base import (
    ActionSpec,
    BaseConnector,
    CircuitBreakerOpenError,
    ConnectorManifest,
    IntegrationError,
    IntegrationNotFoundError,
    ParamSpec,
    PolicyDeniedError,
    RateLimit,
    RiskLevel,
    SecretMissingError,
)
from .circuit_breaker import CircuitBreaker
from .metrics import IntegrationMetrics
from .policy import PolicyEngine
from .registry import IntegrationRegistry
from .skill_bridge import IntegrationSkillBridge
from .vault import SecretVault

__all__ = [
    "ActionSpec",
    "BaseConnector",
    "CircuitBreaker",
    "CircuitBreakerOpenError",
    "ConnectorManifest",
    "IntegrationError",
    "IntegrationMetrics",
    "IntegrationNotFoundError",
    "IntegrationRegistry",
    "IntegrationSkillBridge",
    "ParamSpec",
    "PolicyDeniedError",
    "PolicyEngine",
    "RateLimit",
    "RiskLevel",
    "SecretMissingError",
    "SecretVault",
]
