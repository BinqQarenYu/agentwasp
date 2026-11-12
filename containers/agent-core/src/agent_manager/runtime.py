"""AgentRuntime — per-agent execution context wrapping the shared GoalOrchestrator.

Design:
  - Does NOT create new GoalOrchestrator instances (avoids resource duplication)
  - Injects agent-specific autonomy_mode, CognitiveBudget, and allowed_capabilities
    at goal-creation time
  - Memory is namespaced via project_id=agent.effective_namespace
  - Model override: if agent.model_provider differs from the active provider,
    the runtime temporarily switches (best-effort, logs warning on failure)
  - Subagent Genesis integration: allows root agents (depth=0) to spawn Depth-1
    ephemeral subagents with resource leashes.
"""

from __future__ import annotations

import structlog

from ..goal_orchestrator.orchestrator import GoalOrchestrator
from ..goal_orchestrator.store import load_goal, save_goal
from ..goal_orchestrator.types import AutonomyMode, Goal, GoalState
from .events import AGENT_GOAL_CREATED, AGENT_GOAL_COMPLETED, AGENT_GOAL_FAILED
from .genesis import SubagentGenesis, SubagentTaskReport
from .store import save_agent
from .types import Agent, AgentStatus

logger = structlog.get_logger()


class AgentRuntime:
    """Per-agent execution context.

    One AgentRuntime is created per RUNNING agent and cached in
    AgentOrchestrator._runtimes. The runtime is discarded when the agent
    is paused/archived.
    """

    def __init__(
        self,
        agent: Agent,
        goal_orchestrator: GoalOrchestrator,
        model_manager,
        bus,
        redis_url: str,
    ) -> None:
        self.agent = agent
        self.goal_orchestrator = goal_orchestrator
        self.model_manager = model_manager
        self.bus = bus
        self.redis_url = redis_url
        self._genesis = SubagentGenesis(redis_url=self.redis_url, bus=self.bus)

    @property
    def memory_namespace(self) -> str:
        return self.agent.effective_namespace

    # ------------------------------------------------------------------
    # Goal management
    # ------------------------------------------------------------------

    async def create_goal(
        self,
        objective: str,
        chat_id: str = "",
        constraints: str = "",
        success_criteria: str = "",
        **kwargs,
    ) -> Goal:
        """Create a goal with this agent's autonomy_mode and budget injected."""
        effective_chat_id = chat_id or getattr(self.goal_orchestrator, "default_chat_id", "")
        goal = await self.goal_orchestrator.create_goal(
            objective=objective,
            chat_id=effective_chat_id,
            constraints=constraints,
            success_criteria=success_criteria,
            autonomy_mode=self.agent.autonomy_mode,
            budget_max_tokens_planning=self.agent.cognitive_budget.max_tokens_planning,
            budget_max_replans=self.agent.cognitive_budget.max_replans,
            max_runtime_seconds=self.agent.cognitive_budget.max_runtime_seconds,
            priority=kwargs.pop("priority", 6),
            source=kwargs.pop("source", "agent"),
        )

        # Inject agent sandbox and ownership into the goal
        import redis.asyncio as aioredis
        r = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            goal.allowed_capabilities = list(self.agent.allowed_capabilities)
            goal.agent_id = self.agent.id
            await save_goal(r, goal)

            # Track goal in agent's active list
            if goal.id not in self.agent.active_goal_ids:
                self.agent.active_goal_ids.append(goal.id)
                await save_agent(r, self.agent)
        finally:
            await r.aclose()

        logger.info(
            "agent_runtime.goal_created",
            agent_id=self.agent.id,
            goal_id=goal.id,
            objective=objective[:80],
        )
        await self._emit(AGENT_GOAL_CREATED, detail=f"Goal: {objective[:100]}")
        return goal

    async def tick(self) -> None:
        """Execute one step for each of this agent's active goals."""
        if not self.agent.active_goal_ids:
            return

        import time
        import redis.asyncio as aioredis
        r = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            completed_ids = []
            failed_ids = []
            stuck_ids = []  # PLANNING goals with no tasks for > 60s

            for goal_id in list(self.agent.active_goal_ids):
                goal = await load_goal(r, goal_id)
                if goal is None:
                    completed_ids.append(goal_id)
                    continue

                if goal.state == GoalState.ACTIVE:
                    pass  # goal_tick handles execution; we just watch for terminal states
                elif goal.state == GoalState.PLANNING:
                    # Detect stuck PLANNING goals (no tasks and > 60s old)
                    nodes = goal.task_graph.nodes if goal.task_graph else []
                    if not nodes:
                        created = goal.created_at
                        if hasattr(created, 'timestamp'):
                            age = time.time() - created.timestamp()
                        else:
                            try:
                                from datetime import datetime, timezone
                                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                                age = time.time() - dt.timestamp()
                            except Exception:
                                age = 999  # Unknown age — treat as stuck
                        if age > 60:
                            logger.warning(
                                "agent_runtime.goal_stuck_planning",
                                goal_id=goal_id,
                            )
                            stuck_ids.append(goal_id)
                elif goal.state in (GoalState.COMPLETED, GoalState.FAILED):
                    if goal.state == GoalState.COMPLETED:
                        completed_ids.append(goal_id)
                    else:
                        failed_ids.append(goal_id)

            # Re-create stuck PLANNING goals via the proper planning path
            for goal_id in stuck_ids:
                goal = await load_goal(r, goal_id)
                if goal is None:
                    completed_ids.append(goal_id)
                    continue
                try:
                    new_goal = await self.create_goal(
                        objective=goal.objective,
                        chat_id=goal.chat_id,
                    )
                    logger.info(
                        "agent_runtime.goal_replanned",
                        old_id=goal_id[:8],
                        new_id=new_goal.id[:8],
                    )
                except Exception as _e:
                    logger.exception("agent_runtime.goal_replan_failed", goal_id=goal_id)
                from ..goal_orchestrator.store import delete_goal
                await delete_goal(r, goal_id)
                failed_ids.append(goal_id)

            # Prune terminal goals from active list
            terminal = set(completed_ids + failed_ids)
            if terminal:
                self.agent.active_goal_ids = list(dict.fromkeys(
                    gid for gid in self.agent.active_goal_ids if gid not in terminal
                ))
                if not self.agent.active_goal_ids:
                    self.agent.status = AgentStatus.IDLE
                await save_agent(r, self.agent)
        finally:
            await r.aclose()

    # ------------------------------------------------------------------
    # Bounded Subagent Genesis (Depth-1 Spawner)
    # ------------------------------------------------------------------

    async def spawn_subagent(
        self,
        role: str,
        task_objective: str,
        skills_needed: list[str] | None = None,
        custom_system_prompt: str = "",
        timeout_seconds: int | None = None,
        token_budget: int = 2000,
        model_provider: str = "",
        model_name: str = "",
    ) -> Agent:
        """Spawn an ephemeral subagent bounded strictly to Depth 1."""
        return await self._genesis.spawn_subagent(
            parent=self.agent,
            role=role,
            task_objective=task_objective,
            skills_needed=skills_needed,
            custom_system_prompt=custom_system_prompt,
            timeout_seconds=timeout_seconds,
            token_budget=token_budget,
            model_provider=model_provider,
            model_name=model_name,
        )

    async def reap_subagent(self, subagent_id: str, reason: str = "completed") -> bool:
        """Archive and clean up an ephemeral subagent."""
        return await self._genesis.reap_subagent(
            parent=self.agent,
            subagent_id=subagent_id,
            reason=reason,
        )

    async def run_ephemeral_subagent_task(
        self,
        role: str,
        objective: str,
        skills_needed: list[str] | None = None,
        timeout_seconds: int = 120,
    ) -> SubagentTaskReport:
        """End-to-end execution of a bounded ephemeral subagent task."""
        return await self._genesis.run_subagent_workflow(
            parent=self.agent,
            role=role,
            objective=objective,
            orchestrator=self.goal_orchestrator,
            skills_needed=skills_needed,
            timeout_seconds=timeout_seconds,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _emit(self, event_name: str, detail: str = "") -> None:
        """Emit an AGENT_* event to the outgoing stream (best-effort)."""
        try:
            await self.bus.publish(
                "events:outgoing",
                {
                    "event_type": event_name,
                    "agent_id": self.agent.id,
                    "agent_name": self.agent.name,
                    "detail": detail,
                },
            )
        except Exception:
            logger.exception("agent_runtime.emit_error", ev=event_name)
