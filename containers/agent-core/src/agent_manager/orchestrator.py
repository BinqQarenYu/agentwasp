"""AgentOrchestrator — top-level multi-agent coordinator.

Manages the lifecycle of multiple Agent entities, each with an AgentRuntime
that wraps the shared GoalOrchestrator. Enforces global stability controls:
  - max_active_agents: cap on RUNNING agents
  - max_concurrent_agent_steps: asyncio semaphore across all agent ticks
  - cpu_usage_threshold: skip tick if CPU load is too high (psutil)
  - global_token_budget_per_minute: Redis-based per-minute token cap

All AGENT_* events are emitted to the events:outgoing stream.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import redis.asyncio as aioredis
import structlog

from ..goal_orchestrator.orchestrator import GoalOrchestrator
from .bus import get_messages as bus_get_messages
from .bus import mark_read, send_message as bus_send_message
from .events import (
    AGENT_ARCHIVED,
    AGENT_CPU_THROTTLE,
    AGENT_CREATED,
    AGENT_GLOBAL_THROTTLE,
    AGENT_MESSAGE_SENT,
    AGENT_PAUSED,
    AGENT_RESUMED,
    AGENT_SANDBOX_DENIED,
    AGENT_STARTED,
)
from .runtime import AgentRuntime
from .sandbox import CapabilityDeniedError, CapabilitySandbox
from .store import list_active_agents, list_agents, load_agent, save_agent
from .types import Agent, AgentStatus

logger = structlog.get_logger()

# Redis key for global per-minute token budget
_TOKEN_BUDGET_KEY_PREFIX = "agent:global_tokens:"


class AgentOrchestrator:
    """Coordinates all agent runtimes with global throttle controls.

    Wired up in main.py; passed to AgentTickJob and dashboard routes.
    """

    def __init__(
        self,
        redis_url: str,
        goal_orchestrator: GoalOrchestrator,
        skill_executor,
        memory_manager,
        model_manager,
        bus,
        max_active_agents: int = 10,
        max_concurrent_agent_steps: int = 5,
        cpu_usage_threshold: float = 85.0,
        global_token_budget_per_minute: int = 100_000,
    ) -> None:
        self.redis_url = redis_url
        self.goal_orchestrator = goal_orchestrator
        self.skill_executor = skill_executor
        self.memory_manager = memory_manager
        self.model_manager = model_manager
        self.bus = bus
        self.max_active_agents = max_active_agents
        self.max_concurrent_agent_steps = max_concurrent_agent_steps
        self.cpu_usage_threshold = cpu_usage_threshold
        self.global_token_budget_per_minute = global_token_budget_per_minute

        self._runtimes: dict[str, AgentRuntime] = {}
        self._sandbox = CapabilitySandbox()
        self._step_semaphore = asyncio.Semaphore(max_concurrent_agent_steps)

    # ------------------------------------------------------------------
    # Agent CRUD
    # ------------------------------------------------------------------

    async def create_agent(
        self,
        name: str,
        description: str = "",
        model_provider: str = "",
        model_name: str = "",
        identity_prompt: str = "",
        autonomy_mode: str = "semi",
        allowed_capabilities: list[str] | None = None,
        memory_namespace: str = "",
        metadata: dict | None = None,
    ) -> Agent:
        """Create a new agent and persist it."""
        from ..goal_orchestrator.types import AutonomyMode
        agent = Agent(
            name=name,
            description=description,
            model_provider=model_provider,
            model_name=model_name,
            identity_prompt=identity_prompt,
            autonomy_mode=AutonomyMode(autonomy_mode),
            allowed_capabilities=allowed_capabilities or [],
            memory_namespace=memory_namespace,
            metadata=metadata or {},
        )
        r = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            # Dedup: return existing agent with same name instead of creating duplicate
            existing_agents = await list_agents(r)
            for existing in existing_agents:
                if existing.name.lower() == name.lower():
                    logger.info("agent_orchestrator.dedup_existing", agent_id=existing.id, name=name)
                    return existing
            await save_agent(r, agent)
        finally:
            await r.aclose()

        logger.info("agent_orchestrator.created", agent_id=agent.id, name=name)
        await self._emit(AGENT_CREATED, agent, detail=f"Agent '{name}' created")
        return agent

    async def get_agent(self, agent_id: str) -> Agent | None:
        r = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            return await load_agent(r, agent_id)
        finally:
            await r.aclose()

    async def list_agents(self) -> list[Agent]:
        r = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            return await list_agents(r)
        finally:
            await r.aclose()

    async def pause_agent(self, agent_id: str) -> bool:
        agent = await self.get_agent(agent_id)
        if agent is None or agent.status == AgentStatus.ARCHIVED:
            return False
        agent.status = AgentStatus.PAUSED
        r = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            await save_agent(r, agent)
        finally:
            await r.aclose()
        # Remove runtime cache
        self._runtimes.pop(agent_id, None)
        await self._emit(AGENT_PAUSED, agent, detail="Manually paused")
        logger.info("agent_orchestrator.paused", agent_id=agent_id)
        return True

    async def resume_agent(self, agent_id: str) -> bool:
        agent = await self.get_agent(agent_id)
        if agent is None or agent.status == AgentStatus.ARCHIVED:
            return False
        agent.status = AgentStatus.RUNNING if agent.active_goal_ids else AgentStatus.IDLE
        r = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            await save_agent(r, agent)
        finally:
            await r.aclose()
        await self._emit(AGENT_RESUMED, agent, detail="Manually resumed")
        logger.info("agent_orchestrator.resumed", agent_id=agent_id)
        return True

    async def archive_agent(self, agent_id: str) -> bool:
        agent = await self.get_agent(agent_id)
        if agent is None:
            return False
        agent.status = AgentStatus.ARCHIVED
        r = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            await save_agent(r, agent)
        finally:
            await r.aclose()
        self._runtimes.pop(agent_id, None)
        await self._emit(AGENT_ARCHIVED, agent, detail="Archived")
        logger.info("agent_orchestrator.archived", agent_id=agent_id)
        return True

    async def delete_agent(self, agent_id: str) -> bool:
        """Hard-delete an agent from Redis entirely."""
        from .store import delete_agent as store_delete
        agent = await self.get_agent(agent_id)
        r = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            ok = await store_delete(r, agent_id)
        finally:
            await r.aclose()
        self._runtimes.pop(agent_id, None)
        if ok and agent:
            await self._emit(AGENT_ARCHIVED, agent, detail="Deleted")
            logger.info("agent_orchestrator.deleted", agent_id=agent_id)
        return ok

    async def delete_all_agents(self) -> int:
        """Hard-delete ALL agents. Returns count deleted."""
        from .store import delete_agent as store_delete
        agents = await self.list_agents()
        r = aioredis.from_url(self.redis_url, decode_responses=True)
        count = 0
        try:
            for agent in agents:
                ok = await store_delete(r, agent.id)
                if ok:
                    self._runtimes.pop(agent.id, None)
                    count += 1
        finally:
            await r.aclose()
        logger.info("agent_orchestrator.deleted_all", count=count)
        return count

    # ------------------------------------------------------------------
    # Goal creation via agent
    # ------------------------------------------------------------------

    async def create_agent_goal(
        self,
        agent_id: str,
        objective: str,
        chat_id: str = "",
        **kwargs,
    ):
        """Create a goal on behalf of an agent.

        Checks capability sandbox before delegating to AgentRuntime.
        """
        agent = await self.get_agent(agent_id)
        if agent is None:
            raise ValueError(f"Agent {agent_id} not found")
        if agent.status == AgentStatus.ARCHIVED:
            raise ValueError(f"Agent {agent.name} is archived")

        runtime = self._get_or_create_runtime(agent)
        goal = await runtime.create_goal(objective, chat_id=chat_id, **kwargs)

        # Mark agent as RUNNING
        if agent.status == AgentStatus.IDLE:
            agent.status = AgentStatus.RUNNING
            r = aioredis.from_url(self.redis_url, decode_responses=True)
            try:
                await save_agent(r, agent)
            finally:
                await r.aclose()
            await self._emit(AGENT_STARTED, agent, detail=f"Goal: {objective[:80]}")

        return goal

    # ------------------------------------------------------------------
    # Inter-agent messaging
    # ------------------------------------------------------------------

    async def send_message(
        self,
        from_agent_id: str,
        to_agent_id: str,
        content: str,
        message_type: str = "text",
        metadata: dict | None = None,
    ):
        """Send a message from one agent to another."""
        msg = await bus_send_message(
            from_agent_id=from_agent_id,
            to_agent_id=to_agent_id,
            content=content,
            message_type=message_type,
            metadata=metadata,
        )
        await self._emit(
            AGENT_MESSAGE_SENT,
            agent=None,
            detail=f"From {from_agent_id} to {to_agent_id}: {content[:80]}",
            extra={"from_id": from_agent_id, "to_id": to_agent_id},
        )
        return msg

    async def get_messages(self, agent_id: str, limit: int = 20, unread_only: bool = False):
        return await bus_get_messages(agent_id, limit=limit, unread_only=unread_only)

    async def mark_message_read(self, message_id: str) -> None:
        await mark_read(message_id)

    # ------------------------------------------------------------------
    # Tick — called by AgentTickJob every N seconds
    # ------------------------------------------------------------------

    async def tick(self) -> None:
        """Execute one step per active RUNNING agent, with global guards."""

        # ── CPU backpressure ───────────────────────────────────────────
        try:
            import asyncio as _asyncio
            import psutil
            cpu = await _asyncio.to_thread(psutil.cpu_percent, interval=0.1)
            if cpu > self.cpu_usage_threshold:
                logger.warning(
                    "agent_orchestrator.cpu_throttle",
                    cpu=cpu,
                    threshold=self.cpu_usage_threshold,
                )
                await self._emit(
                    AGENT_CPU_THROTTLE,
                    agent=None,
                    detail=f"CPU at {cpu:.1f}% > {self.cpu_usage_threshold}%",
                )
                return
        except Exception:
            pass  # psutil failure is non-fatal

        # ── Global token budget ────────────────────────────────────────
        r = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            minute_key = _TOKEN_BUDGET_KEY_PREFIX + datetime.now(timezone.utc).strftime("%Y-%m-%d-%H-%M")
            current = await r.get(minute_key)
            if current and int(current) >= self.global_token_budget_per_minute:
                logger.warning(
                    "agent_orchestrator.global_throttle",
                    tokens=int(current),
                    budget=self.global_token_budget_per_minute,
                )
                await self._emit(
                    AGENT_GLOBAL_THROTTLE,
                    agent=None,
                    detail=f"Global token budget {current}/{self.global_token_budget_per_minute} exceeded",
                )
                return

            # ── Tick active agents ─────────────────────────────────────
            active_agents = await list_active_agents(r)
            if not active_agents:
                return

            async def _tick_one(agent: Agent) -> None:
                runtime = self._get_or_create_runtime(agent)
                async with self._step_semaphore:
                    try:
                        await runtime.tick()
                    except Exception:
                        logger.exception(
                            "agent_orchestrator.tick_error",
                            agent_id=agent.id,
                        )

            await asyncio.gather(*[_tick_one(a) for a in active_agents])
        finally:
            await r.aclose()

    # ------------------------------------------------------------------
    # Capability sandbox (public utility)
    # ------------------------------------------------------------------

    def check_capability(self, agent: Agent, capability_name: str) -> bool:
        return self._sandbox.check(agent, capability_name)

    def gate_capability(self, agent: Agent, capability_name: str) -> None:
        """Raise CapabilityDeniedError if not allowed."""
        self._sandbox.gate(agent, capability_name)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_or_create_runtime(self, agent: Agent) -> AgentRuntime:
        if agent.id not in self._runtimes:
            self._runtimes[agent.id] = AgentRuntime(
                agent=agent,
                goal_orchestrator=self.goal_orchestrator,
                model_manager=self.model_manager,
                bus=self.bus,
                redis_url=self.redis_url,
            )
        else:
            # Keep runtime agent reference up to date
            self._runtimes[agent.id].agent = agent
        return self._runtimes[agent.id]

    async def _emit(
        self,
        event_name: str,
        agent: Agent | None,
        detail: str = "",
        extra: dict | None = None,
    ) -> None:
        """Publish an AGENT_* event to events:outgoing (best-effort)."""
        try:
            payload = {
                "event_type": event_name,
                "detail": detail,
                "correlation_id": str(uuid4()),
            }
            if agent is not None:
                payload["agent_id"] = agent.id
                payload["agent_name"] = agent.name
            if extra:
                payload.update(extra)
            await self.bus.publish("events:outgoing", payload)
        except Exception:
            logger.warning("agent_orchestrator.emit_error", ev=event_name)
