"""Integration skill — bridges LLM calls to the IntegrationRegistry.

The LLM calls:
    integration(connector="slack", action="send_message", params={"text": "Hello!"})

All secret resolution and policy enforcement happens inside the bridge/registry.
The LLM only supplies connector name, action name, and non-secret params.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from ..base import SkillBase
from ..types import ParamType, SkillDefinition, SkillParam, SkillResult

logger = structlog.get_logger()


class IntegrationSkill(SkillBase):
    """Wraps IntegrationSkillBridge as a standard WASP skill."""

    def __init__(self, bridge) -> None:
        self._bridge = bridge

    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="integration",
            description=(
                "Execute an external integration action (Slack, Discord, GitHub, Notion, "
                "Home Assistant, Zapier, webhook, MCP, and more). "
                "Use connector='wasp_integrations', action='list_integrations' to discover available options. "
                "Secrets are handled automatically — never include them in params."
            ),
            params=[
                SkillParam(
                    name="connector",
                    param_type=ParamType.STRING,
                    description="Integration ID (e.g. 'slack', 'github', 'discord'). Use 'wasp_integrations' for meta-actions.",
                ),
                SkillParam(
                    name="action",
                    param_type=ParamType.STRING,
                    description="Action to perform (e.g. 'send_message', 'create_issue'). Use 'list_integrations' with connector='wasp_integrations' to see all.",
                ),
                SkillParam(
                    name="params",
                    param_type=ParamType.STRING,
                    description="Action parameters as a JSON object string. Do NOT include secrets.",
                    required=False,
                    default="{}",
                ),
            ],
            category="integrations",
            timeout_seconds=30.0,
        )

    async def execute(
        self,
        connector: str,
        action: str,
        params: str | dict | None = None,
        **kwargs: Any,
    ) -> SkillResult:
        # Normalize params — may arrive as JSON string or dict
        if isinstance(params, str):
            try:
                params_dict = json.loads(params) if params.strip() else {}
            except Exception:
                params_dict = {}
        elif isinstance(params, dict):
            params_dict = params
        else:
            params_dict = {}

        try:
            result = await self._bridge.execute({
                "connector": connector,
                "action":    action,
                "params":    params_dict,
            })
            if result.get("ok"):
                data = result.get("data", {})
                # Format nicely for the LLM
                return SkillResult(
                    skill_name="integration",
                    success=True,
                    output=json.dumps(data, ensure_ascii=False, indent=2) if data else "OK",
                )
            else:
                return SkillResult(
                    skill_name="integration",
                    success=False,
                    output=result.get("error", "Integration failed"),
                )
        except Exception as exc:
            logger.error("integration_skill.execute_error", error=str(exc))
            return SkillResult(skill_name="integration", success=False, output=f"Integration error: {exc}")
