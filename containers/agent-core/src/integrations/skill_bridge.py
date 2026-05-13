"""Integration skill bridge — exposes IntegrationRegistry as a WASP skill.

The LLM calls:
    integration(connector="slack", action="send_message", params={...})

The bridge:
1. Validates connector + action exist
2. Enforces policy (risk gate)
3. Resolves secrets from vault (LLM never sees them)
4. Executes via circuit breaker
5. Returns structured result to the agent

This is the ONLY path through which the LLM can trigger integrations.
"""

from __future__ import annotations

from typing import Any

import structlog

from .base import (
    ActionNotFoundError,
    CircuitBreakerOpenError,
    IntegrationError,
    IntegrationNotFoundError,
    PolicyDeniedError,
    SecretMissingError,
)
from .registry import IntegrationRegistry

logger = structlog.get_logger()


class IntegrationSkillBridge:
    """Thin adapter between WASP SkillBase and IntegrationRegistry."""

    skill_name = "integration"
    description = (
        "Execute an external integration action (Slack, Discord, GitHub, Notion, "
        "Home Assistant, Zapier, webhook, MCP, and more). "
        "Use list_integrations to discover available connectors and their actions."
    )

    def __init__(self, registry: IntegrationRegistry) -> None:
        self._registry = registry

    # ------------------------------------------------------------------
    # Called by SkillExecutor
    # ------------------------------------------------------------------

    async def execute(self, params: dict[str, Any]) -> dict[str, Any]:
        """Entry point for skill execution.

        Expected params:
            connector  — Integration ID (e.g. "slack")
            action     — Action ID (e.g. "send_message")
            params     — Optional dict of action parameters (LLM-supplied, no secrets)

        Special actions on connector="wasp_integrations":
            list_integrations — List all registered integrations + their actions
            get_manifest      — Get full manifest for one integration
        """
        connector = str(params.get("connector") or "").strip()
        action    = str(params.get("action")    or "").strip()
        sub_params = dict(params.get("params") or {})

        # Meta-actions
        if connector == "wasp_integrations":
            return self._meta_action(action, sub_params)

        if not connector:
            return self._err("connector is required. Use connector='wasp_integrations', action='list_integrations' to see options.")
        if not action:
            return self._err(f"action is required for connector '{connector}'.")

        try:
            result = await self._registry.execute(connector, action, sub_params)
            return result

        except IntegrationNotFoundError:
            available = [i["id"] for i in self._registry.list_integrations()]
            return self._err(
                f"Integration '{connector}' not found. Available: {available}. "
                "Configure secrets at /integrations in the dashboard first."
            )
        except ActionNotFoundError as exc:
            manifest = self._registry.get_manifest(connector)
            actions = [a.id for a in manifest.actions]
            return self._err(f"Action '{action}' not found in '{connector}'. Available: {actions}")
        except PolicyDeniedError as exc:
            return self._err(f"Policy denied: {exc}")
        except SecretMissingError as exc:
            return self._err(
                f"{exc}. Configure secrets at /integrations in the dashboard."
            )
        except CircuitBreakerOpenError as exc:
            return self._err(
                f"Circuit breaker is OPEN for '{connector}' — too many recent failures. "
                "Wait a few minutes or reset from /integrations dashboard."
            )
        except IntegrationError as exc:
            return self._err(str(exc))
        except Exception as exc:
            logger.error("integration_skill.unexpected_error", connector=connector, action=action, error=str(exc))
            return self._err(f"Unexpected error: {exc}")

    def _meta_action(self, action: str, params: dict) -> dict:
        if action == "list_integrations":
            integrations = self._registry.list_integrations()
            summary = []
            for i in integrations:
                summary.append({
                    "id":          i["id"],
                    "name":        i["name"],
                    "category":    i["category"],
                    "description": i["description"],
                    "enabled":     i["enabled"],
                    "actions":     [{"id": a["id"], "description": a["description"]} for a in i["actions"]],
                    "secrets_required": i["required_secrets"],
                })
            return {"ok": True, "data": {"integrations": summary, "count": len(summary)}}

        if action == "get_manifest":
            integration_id = params.get("integration_id", "")
            if not integration_id:
                return self._err("integration_id is required")
            try:
                m = self._registry.get_manifest(integration_id)
                return {"ok": True, "data": {
                    "id":               m.id,
                    "name":             m.name,
                    "version":          m.version,
                    "description":      m.description,
                    "risk_level":       m.risk_level.value,
                    "required_secrets": m.required_secrets,
                    "actions":          [
                        {"id": a.id, "description": a.description,
                         "risk_level": a.risk_level.value,
                         "params": [{"name": p.name, "type": p.type, "required": p.required, "description": p.description}
                                    for p in a.params]}
                        for a in m.actions
                    ],
                }}
            except IntegrationNotFoundError:
                return self._err(f"Integration '{integration_id}' not found")

        return self._err(f"Unknown meta-action '{action}'. Available: list_integrations, get_manifest")

    @staticmethod
    def _err(message: str) -> dict:
        return {"ok": False, "error": message}


def format_for_skill_context(registry: IntegrationRegistry) -> str:
    """Generate a brief summary of enabled integrations for injection into the system prompt."""
    integrations = registry.list_integrations()
    enabled = [i for i in integrations if i["enabled"]]
    if not enabled:
        return ""

    lines = ["INTEGRATIONS AVAILABLE (call via integration skill):"]
    for i in enabled:
        action_ids = ", ".join(a["id"] for a in i["actions"])
        lines.append(f"  - {i['id']} ({i['name']}): {action_ids}")
    lines.append(
        "\nTo use: integration(connector='CONNECTOR_ID', action='ACTION_ID', params={{...}})"
        "\nTo list: integration(connector='wasp_integrations', action='list_integrations')"
    )
    return "\n".join(lines)
