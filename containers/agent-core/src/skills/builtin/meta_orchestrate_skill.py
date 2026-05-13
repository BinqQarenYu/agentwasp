"""MetaOrchestrate Skill — System 3 (Meta-Agent Supervisor) LLM interface.

Allows the LLM to decompose complex goals into coordinated agent teams.
Late-wired to MetaSupervisor instance in main.py after initialization.

Example usage:
  meta_orchestrate(
    goal="Analyse the crypto market this week and produce a PDF report",
    description="Market analysis team"
  )
"""

from __future__ import annotations

import structlog

from ..base import SkillBase
from ..types import SkillDefinition, SkillParam, SkillResult

logger = structlog.get_logger()

_SKILL_NAME = "meta_orchestrate"


class MetaOrchestrateSkill(SkillBase):
    """Decomposes complex goals into teams of specialised sub-agents."""

    def __init__(self, meta_supervisor=None):
        self._supervisor = meta_supervisor

    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name=_SKILL_NAME,
            description=(
                "Decompose a complex goal into a coordinated team of specialised agents "
                "that work in parallel. Use when a goal requires multiple distinct areas "
                "of expertise (research + analysis + reporting, etc.)."
            ),
            params=[
                SkillParam(
                    name="goal",
                    description="The complex high-level goal to decompose and execute",
                    required=True,
                ),
                SkillParam(
                    name="description",
                    description="Brief description of what the team should accomplish",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="chat_id",
                    description="Chat ID for progress notifications",
                    required=False,
                    default="",
                ),
            ],
        )

    async def execute(self, **params) -> SkillResult:
        if self._supervisor is None:
            return SkillResult(
                skill_name=_SKILL_NAME,
                output=(
                    "Meta-Agent Supervisor not available. "
                    "Set META_AGENT_ENABLED=true and restart."
                ),
                success=False,
                error="meta_supervisor not initialized",
            )

        goal = params.get("goal", "").strip()
        chat_id = params.get("chat_id", "").strip()

        if not goal:
            return SkillResult(
                skill_name=_SKILL_NAME,
                output="Goal parameter is required.",
                success=False,
            )

        try:
            result = await self._supervisor.execute_team(goal, chat_id=chat_id)

            if not result.get("ok"):
                return SkillResult(
                    skill_name=_SKILL_NAME,
                    output=f"Team execution failed: {result.get('error', 'unknown')}",
                    success=False,
                )

            agents_list = ", ".join(result.get("agents", []))
            synthesis = result.get("synthesis", "")
            lines = [
                f"✅ Meta-agent team completed ({result.get('latency_ms', 0)}ms)",
                f"Team: {result.get('team_name', '')}",
                f"Agents: {agents_list}",
                "",
                "SYNTHESIS:",
                synthesis,
            ]
            return SkillResult(
                skill_name=_SKILL_NAME,
                output="\n".join(lines),
                success=True,
            )

        except Exception as exc:
            logger.exception("meta_orchestrate_skill.error")
            return SkillResult(
                skill_name=_SKILL_NAME,
                output=f"Error: {exc}",
                success=False,
                error=str(exc),
            )
