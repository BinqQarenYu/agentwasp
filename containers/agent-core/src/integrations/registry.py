"""IntegrationRegistry — central orchestrator for all connectors.

Enforces (in order on every execute call):
    1. Integration existence
    2. Action existence
    3. PolicyEngine gate (risk level + enabled state)
    4. Required secrets present in vault
    5. CircuitBreaker (prevent cascading failures)
    6. Connector.execute() with secrets injected
    7. Metrics recording

LLM NEVER accesses the vault; secrets flow only into connector.execute()
and are discarded after the call.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from .base import (
    ActionNotFoundError,
    BaseConnector,
    CircuitBreakerOpenError,
    ConnectorManifest,
    IntegrationError,
    IntegrationNotFoundError,
    PolicyDeniedError,
    SecretMissingError,
)
from .circuit_breaker import CircuitBreaker
from .metrics import IntegrationMetrics
from .policy import PolicyEngine
from .vault import SecretVault

logger = structlog.get_logger()


class IntegrationRegistry:
    """Thread-safe (asyncio) registry for WASP connectors."""

    def __init__(
        self,
        vault: SecretVault,
        policy: PolicyEngine,
        cb_failure_threshold: int = 5,
        cb_recovery_timeout: float = 60.0,
        redis_url: str = "",
    ) -> None:
        self._vault     = vault
        self._policy    = policy
        self._cb_ft     = cb_failure_threshold
        self._cb_rt     = cb_recovery_timeout
        self._redis_url = redis_url

        self._connectors: dict[str, BaseConnector]       = {}
        self._breakers:   dict[str, CircuitBreaker]      = {}
        self._metrics:    dict[str, IntegrationMetrics]  = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, connector: BaseConnector) -> None:
        """Register a connector. Safe to call multiple times (idempotent)."""
        m   = connector.manifest()
        iid = m.id
        self._connectors[iid] = connector
        self._breakers[iid]   = CircuitBreaker(iid, self._cb_ft, self._cb_rt, self._redis_url)
        self._metrics[iid]    = IntegrationMetrics(iid)
        logger.info(
            "integration_registry.registered",
            id=iid,
            name=m.name,
            category=m.category,
            actions=len(m.actions),
        )

    # ------------------------------------------------------------------
    # Execution (the only public path for running an integration)
    # ------------------------------------------------------------------

    async def execute(
        self,
        integration_id: str,
        action: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute an integration action — the only LLM-callable entry point.

        Secrets are NEVER in params (LLM-supplied); they are injected from
        the vault and passed directly to connector.execute().
        """
        params = params or {}

        # 1. Lookup
        if integration_id not in self._connectors:
            raise IntegrationNotFoundError(integration_id)
        connector = self._connectors[integration_id]
        manifest  = connector.manifest()

        # 2. Action lookup
        action_spec = next((a for a in manifest.actions if a.id == action), None)
        if action_spec is None:
            raise ActionNotFoundError(action, integration_id)

        # 3. Policy gate
        gate = await self._policy.gate(integration_id, action, action_spec.risk_level)
        if not gate.allowed:
            raise PolicyDeniedError(gate.reason)

        # 4. Resolve secrets from vault (NEVER goes to LLM)
        secrets = await self._vault.get_all(integration_id)

        # 5. Required secrets present?
        for req in manifest.required_secrets:
            if req not in secrets:
                raise SecretMissingError(integration_id, req)

        # 6. Execute via circuit breaker
        cb      = self._breakers[integration_id]
        metrics = self._metrics[integration_id]
        start   = time.monotonic()
        try:
            result      = await cb.call(connector.execute, action, params, secrets)
            latency_ms  = (time.monotonic() - start) * 1_000
            await metrics.record_success(latency_ms)
            logger.info(
                "integration_registry.executed",
                integration=integration_id,
                action=action,
                latency_ms=round(latency_ms, 1),
            )
            return result

        except (CircuitBreakerOpenError, PolicyDeniedError, SecretMissingError,
                ActionNotFoundError, IntegrationNotFoundError):
            raise

        except Exception as exc:
            latency_ms = (time.monotonic() - start) * 1_000
            await metrics.record_failure(str(exc), latency_ms)
            logger.error(
                "integration_registry.execution_failed",
                integration=integration_id,
                action=action,
                error=str(exc),
            )
            raise IntegrationError(
                f"Integration '{integration_id}' action '{action}' failed: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Health checks
    # ------------------------------------------------------------------

    async def health_check_all(self) -> dict[str, bool]:
        """Run health checks for all enabled integrations (10s timeout each)."""
        results: dict[str, bool] = {}
        for iid, connector in self._connectors.items():
            if not self._policy.is_enabled(iid):
                results[iid] = False
                continue
            try:
                results[iid] = await asyncio.wait_for(connector.health_check(), timeout=10.0)
            except Exception:
                results[iid] = False
        return results

    # ------------------------------------------------------------------
    # Introspection (for dashboard)
    # ------------------------------------------------------------------

    def list_integrations(self) -> list[dict[str, Any]]:
        """Return full status for all registered integrations."""
        out = []
        for iid, connector in self._connectors.items():
            m  = connector.manifest()
            cb = self._breakers[iid]
            mt = self._metrics[iid]
            out.append({
                "id":               iid,
                "name":             m.name,
                "category":         m.category,
                "description":      m.description,
                "version":          m.version,
                "risk_level":       m.risk_level.value,
                "capabilities":     m.capabilities,
                "actions":          [
                    {"id": a.id, "description": a.description,
                     "risk_level": a.risk_level.value, "capability": a.capability}
                    for a in m.actions
                ],
                "required_secrets": m.required_secrets,
                "enabled":          self._policy.is_enabled(iid),
                "circuit_breaker":  cb.to_dict(),
                "metrics":          mt.snapshot(),
                "sparkline":        mt.sparkline_data(),
            })
        return sorted(out, key=lambda x: (x["category"], x["name"]))

    def get_manifest(self, integration_id: str) -> ConnectorManifest:
        if integration_id not in self._connectors:
            raise IntegrationNotFoundError(integration_id)
        return self._connectors[integration_id].manifest()

    @property
    def vault(self) -> SecretVault:
        return self._vault

    @property
    def policy(self) -> PolicyEngine:
        return self._policy
