"""System 3 — Meta-Agent Supervisor.

Enables complex goal decomposition into coordinated teams of specialised agents.

Architecture:
  User goal → MetaSupervisor → sub-agent team creation → AgentOrchestrator
    → parallel execution → result aggregation → synthesis

Workflow example:
  "Analyse crypto market and produce report" →
    research_agent   (web_search, fetch_url)
    data_agent       (python_exec, calculate)
    analysis_agent   (python_exec, web_search)
    report_agent     (gmail, write_file)
  → supervisor polls progress → final aggregated report

Integration:
  - MetaOrchestrateSKill: callable by the LLM as `meta_orchestrate(goal=..., roles=[...])`
  - MetaSupervisor: Python class, injected into skill via late-wiring in main.py
  - Integrates with existing AgentOrchestrator (no new infrastructure needed)
  - Feature flag: META_AGENT_ENABLED=false → skill returns error message

Safety:
  - MAX_AGENT_TEAM_SIZE caps simultaneous agent count
  - All sub-agents use the shared goal_orchestrator (subject to existing budgets)
  - Sub-agents are archived after task completion
  - Supervisor publishes audit events to EventBus
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import uuid4

import structlog

if TYPE_CHECKING:
    from .orchestrator import AgentOrchestrator
    from ..models.manager import ModelManager

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Decomposition prompt
# ---------------------------------------------------------------------------

_DECOMPOSE_SYSTEM = """You are a task decomposition expert for a multi-agent AI platform.

Given a complex goal, decompose it into a team of 2-5 specialised agents.
Each agent has a specific role and a clear, actionable objective.

Return ONLY valid JSON:
{
  "team_name": "short_team_identifier",
  "agents": [
    {
      "name": "AgentName",
      "role": "clear role description (1 sentence)",
      "objective": "specific task this agent must complete",
      "skills": ["skill1", "skill2"],
      "depends_on": []
    }
  ],
  "aggregation_strategy": "how to combine results (1 sentence)"
}

Rules:
- 2 to 5 agents maximum
- Each agent must have a distinct, non-overlapping role
- Dependencies must reference agent names in the same team
- Skills must be from: web_search, fetch_url, browser, python_exec, shell,
  calculate, gmail, write_file, read_file, http_request, task_manager
- Prefer parallel agents (depends_on: []) for speed
"""


class MetaSupervisor:
    """Orchestrates a team of specialised agents for complex goal decomposition."""

    def __init__(
        self,
        agent_orchestrator: AgentOrchestrator,
        model_manager: ModelManager,
        max_team_size: int = 5,
    ):
        self._orch = agent_orchestrator
        self._model = model_manager
        self._max_team_size = max_team_size

    # ------------------------------------------------------------------
    # Decomposition
    # ------------------------------------------------------------------

    async def decompose_goal(self, goal: str) -> dict | None:
        """Ask LLM to decompose a complex goal into a team spec."""
        from ..models.types import Message, ModelRequest

        resp = await self._model.generate(
            ModelRequest(
                messages=[
                    Message(role="system", content=_DECOMPOSE_SYSTEM),
                    Message(role="user", content=f"Complex goal: {goal}"),
                ],
                max_tokens=800,
                temperature=0.3,
            )
        )

        raw = resp.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1].lstrip("json").strip().rstrip("`").strip()
        try:
            spec = json.loads(raw)
            agents = spec.get("agents", [])
            if len(agents) > self._max_team_size:
                spec["agents"] = agents[: self._max_team_size]
            return spec
        except Exception as exc:
            logger.warning("meta_agent.decompose_failed", error=str(exc)[:120])
            return None

    # ------------------------------------------------------------------
    # Team execution
    # ------------------------------------------------------------------

    async def execute_team(self, goal: str, chat_id: str = "") -> dict:
        """Decompose and execute a goal via a coordinated agent team.

        Returns a summary dict with agent results and final synthesis.
        """
        t0 = time.monotonic()
        supervision_id = str(uuid4())[:8]

        logger.info("meta_agent.team_starting", supervision_id=supervision_id, goal=goal[:80])

        # Decompose
        spec = await self.decompose_goal(goal)
        if not spec:
            return {"ok": False, "error": "Failed to decompose goal into agent team"}

        team_agents: dict[str, object] = {}  # name → Agent object
        team_goals: dict[str, str] = {}  # name → goal_id
        results: dict[str, str] = {}  # name → output

        # Create agents in dependency order
        agents_spec = spec.get("agents", [])
        for agent_spec in agents_spec:
            name = agent_spec.get("name", f"Agent_{len(team_agents) + 1}")
            role = agent_spec.get("role", "")
            objective = agent_spec.get("objective", goal)
            identity_prompt = (
                f"You are {name}, a specialised agent with role: {role}.\n"
                f"Your specific task: {objective}\n"
                f"Available skills: {', '.join(agent_spec.get('skills', []))}"
            )

            try:
                agent = await self._orch.create_agent(
                    name=f"meta_{supervision_id}_{name[:20]}",
                    description=role,
                    identity_prompt=identity_prompt,
                    autonomy_mode="full",
                    metadata={"supervision_id": supervision_id, "role": name},
                )
                team_agents[name] = agent

                # Wait for dependencies to complete
                deps = agent_spec.get("depends_on", [])
                if deps:
                    for dep_name in deps:
                        if dep_name in results:
                            continue
                        # Wait for dep agent to finish (poll with timeout)
                        await self._wait_for_agent(team_agents.get(dep_name))

                # Create goal for this agent
                goal_obj = await self._orch.create_agent_goal(
                    agent.id, objective=objective, chat_id=chat_id
                )
                team_goals[name] = goal_obj.id if goal_obj else ""

            except Exception as exc:
                logger.warning("meta_agent.agent_create_failed", agent=name, error=str(exc)[:120])

        # Poll for completion (up to 10 min)
        deadline = asyncio.get_event_loop().time() + 600
        while asyncio.get_event_loop().time() < deadline:
            all_done = True
            for agent_name, agent in team_agents.items():
                goal_id = team_goals.get(agent_name, "")
                if not goal_id:
                    results[agent_name] = "No goal created"
                    continue
                status = await self._get_goal_status(goal_id)
                if status in ("completed", "failed"):
                    if agent_name not in results:
                        results[agent_name] = await self._get_goal_output(goal_id)
                else:
                    all_done = False
            if all_done:
                break
            await asyncio.sleep(15)

        # Archive all sub-agents
        for agent in team_agents.values():
            try:
                await self._orch.archive_agent(agent.id)
            except Exception:
                pass

        # Synthesise results
        synthesis = await self._synthesise(goal, spec, results)

        latency_ms = round((time.monotonic() - t0) * 1000)
        logger.info(
            "meta_agent.team_complete",
            supervision_id=supervision_id,
            agents=len(team_agents),
            latency_ms=latency_ms,
        )

        return {
            "ok": True,
            "supervision_id": supervision_id,
            "team_name": spec.get("team_name", "team"),
            "agents": list(team_agents.keys()),
            "results": results,
            "synthesis": synthesis,
            "latency_ms": latency_ms,
        }

    async def _wait_for_agent(self, agent, timeout: float = 120.0):
        """Wait for an agent to become IDLE/ARCHIVED (dependency resolution)."""
        if agent is None:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                fresh = await self._orch.get_agent(agent.id)
                if fresh and fresh.status.value in ("idle", "archived"):
                    return
            except Exception:
                pass
            await asyncio.sleep(5)

    async def _get_goal_status(self, goal_id: str) -> str:
        """Return goal state string from GoalOrchestrator."""
        try:
            goal = await self._orch.goal_orchestrator.load_goal(goal_id)
            if goal:
                return goal.state.value
        except Exception:
            pass
        return "unknown"

    async def _get_goal_output(self, goal_id: str) -> str:
        """Return summary of goal task outputs."""
        try:
            goal = await self._orch.goal_orchestrator.load_goal(goal_id)
            if goal and goal.task_graph:
                outputs = []
                for node in goal.task_graph.nodes:
                    if node.output_summary:
                        outputs.append(f"[{node.skill_name}] {node.output_summary[:200]}")
                return "\n".join(outputs) if outputs else "No output"
        except Exception:
            pass
        return "Unknown"

    async def _synthesise(
        self, goal: str, spec: dict, results: dict[str, str]
    ) -> str:
        """Ask LLM to synthesise agent outputs into a final answer."""
        from ..models.types import Message, ModelRequest

        results_text = "\n\n".join(
            f"--- {name} ---\n{output}" for name, output in results.items()
        )
        strategy = spec.get("aggregation_strategy", "Combine all results coherently.")

        prompt = (
            f"Original goal: {goal}\n\n"
            f"Agent team results:\n{results_text[:3000]}\n\n"
            f"Aggregation strategy: {strategy}\n\n"
            "Synthesise these results into a final, coherent response. Be concise."
        )

        try:
            resp = await self._model.generate(
                ModelRequest(
                    messages=[Message(role="user", content=prompt)],
                    max_tokens=500,
                )
            )
            return resp.content.strip()
        except Exception:
            return f"Results collected from {len(results)} agents. See individual outputs."
