"""AgentRuntime — per-agent execution context wrapping the shared GoalOrchestrator.

Design:
  - Does NOT create new GoalOrchestrator instances (avoids resource duplication)
  - Injects agent-specific autonomy_mode, CognitiveBudget, and allowed_capabilities
    at goal-creation time
  - Memory is namespaced via project_id=agent.effective_namespace
  - Model override: if agent.model_provider differs from the active provider,
    the runtime temporarily switches (best-effort, logs warning on failure)
"""

from __future__ import annotations

import structlog

from ..goal_orchestrator.orchestrator import GoalOrchestrator
from ..goal_orchestrator.store import load_goal, save_goal
from ..goal_orchestrator.types import AutonomyMode, Goal, GoalState
from .events import AGENT_GOAL_CREATED, AGENT_GOAL_COMPLETED, AGENT_GOAL_FAILED
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
        # Fall back to orchestrator's default_chat_id so agent goal notifications
        # always reach the user even when no explicit chat_id is provided.
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
            # Agent-managed goals rank below user-interactive goals (8) but above
            # fully autonomous background goals (3)
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
                    # goal_tick advances ALL active goals (including agent-owned ones).
                    # agent_tick must NOT call invoke() — that causes double-execution
                    # which triggers skill cooldowns, replan storms, and wasted tokens.
                    # We only observe the goal's state here for cleanup purposes.
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
                            # Stuck: re-create goal via proper orchestrator path
                            logger.warning(
                                "agent_runtime.goal_stuck_planning",
                                goal_id=goal_id,
                                age_s=int(age),
                            )
                            stuck_ids.append(goal_id)
                elif goal.state == GoalState.PAUSED:
                    # PAUSED goals (stability intervention): check if backoff expired then resume,
                    # otherwise treat as failed after 10 minutes to unblock the agent.
                    try:
                        from datetime import datetime, timezone
                        updated = goal.updated_at
                        if hasattr(updated, 'timestamp'):
                            paused_age = time.time() - updated.timestamp()
                        else:
                            dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                            paused_age = time.time() - dt.timestamp()
                    except Exception:
                        paused_age = 999
                    # Try to resume if backoff has expired
                    if goal.stability and goal.stability.backoff_until:
                        try:
                            from datetime import datetime, timezone
                            backoff_until = datetime.fromisoformat(
                                goal.stability.backoff_until.replace("Z", "+00:00")
                            )
                            if datetime.now(timezone.utc) > backoff_until:
                                # Backoff expired — resume goal
                                goal.state = GoalState.ACTIVE
                                goal.stability.in_backoff = False
                                goal.stability.backoff_until = None
                                goal.stability.consecutive_failures = 0
                                await save_goal(r, goal)
                                logger.info(
                                    "agent_runtime.goal_resumed_after_backoff",
                                    goal_id=goal_id[:8],
                                )
                                # Don't add to terminal — it's now active
                                continue
                        except Exception:
                            pass
                    # Give up after 10 minutes paused
                    if paused_age > 600:
                        logger.warning(
                            "agent_runtime.goal_paused_too_long",
                            goal_id=goal_id[:8],
                            paused_age_s=int(paused_age),
                        )
                        failed_ids.append(goal_id)
                        await self._emit(AGENT_GOAL_FAILED, detail=f"Goal paused too long (stability): {goal.objective[:80]}")
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
                # Remove the stuck goal regardless
                from ..goal_orchestrator.store import delete_goal
                await delete_goal(r, goal_id)
                failed_ids.append(goal_id)

            # Prune terminal goals from active list (deduplicate via dict.fromkeys to
            # guard against double-entries that can occur when create_goal saves an
            # intermediate state before the stuck goal is removed from the list)
            terminal = set(completed_ids + failed_ids)
            if terminal:
                self.agent.active_goal_ids = list(dict.fromkeys(
                    gid for gid in self.agent.active_goal_ids if gid not in terminal
                ))
                # Update status if no more active goals
                if not self.agent.active_goal_ids:
                    self.agent.status = AgentStatus.IDLE
                await save_agent(r, self.agent)
        finally:
            await r.aclose()

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
