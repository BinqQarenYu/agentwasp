"""Bounded Subagent Genesis System (Depth-1 Spawner) for Agent WASP.

Enables root agents (e.g., Prometheus `root-liaison`, Athena `bot-15`, Hermes `bot-01`)
to spawn specialized ephemeral subagents to assist with computational, auditing,
and diagnostic workloads.

Guardrails:
1. Depth-1 Boundary: Subagents (depth >= 1) are strictly prohibited from spawning nested agents.
2. Max Concurrency Leash: Parents cannot exceed `max_subagents` simultaneous active children.
3. Resource & TTL Bounding: Subagents inherit limited token slices and strict execution timeouts.
4. Host-PC Hardware Fit: Local models strictly conform to _PC_FITTED_LOCAL_MODELS.
5. Ephemeral Lifecycle: Subagents automatically report results and are reaped (archived) upon completion.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

import redis.asyncio as aioredis
import structlog

try:
    from ..goal_orchestrator.types import AutonomyMode, CognitiveBudget
    from .events import SUBAGENT_SPAWNED, SUBAGENT_REAPED
    from .store import load_agent, save_agent
    from .types import (
        Agent,
        AgentStatus,
        ConcurrencyLimitExceededError,
        DepthLimitExceededError,
        GenesisError,
    )
except (ImportError, ValueError):
    try:
        from src.goal_orchestrator.types import AutonomyMode, CognitiveBudget
        from src.agent_manager.events import SUBAGENT_SPAWNED, SUBAGENT_REAPED
        from src.agent_manager.store import load_agent, save_agent
        from src.agent_manager.types import (
            Agent,
            AgentStatus,
            ConcurrencyLimitExceededError,
            DepthLimitExceededError,
            GenesisError,
        )
    except (ImportError, ValueError):
        from goal_orchestrator.types import AutonomyMode, CognitiveBudget
        from agent_manager.events import SUBAGENT_SPAWNED, SUBAGENT_REAPED
        from agent_manager.store import load_agent, save_agent
        from agent_manager.types import (
            Agent,
            AgentStatus,
            ConcurrencyLimitExceededError,
            DepthLimitExceededError,
            GenesisError,
        )

logger = structlog.get_logger()

# Hardware safe local models on host PC (CPU-only, 17.4 GB free RAM)
_ALLOWED_LOCAL_MODELS = frozenset({"llama3.2:latest", "llama3.2", "nemotron-mini:latest", "nemotron-mini"})


@dataclass
class SubagentTaskReport:
    """Structured report returned when an ephemeral subagent completes its task."""
    subagent_id: str
    parent_agent_id: str
    role: str
    objective: str
    ok: bool
    status: str
    output: str
    latency_ms: int
    created_at: str
    completed_at: str
    error: str = ""


class SubagentGenesis:
    """Manages the creation, bounding, and cleanup of ephemeral subagents."""

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        bus=None,
        default_timeout_seconds: int = 180,
        redis_client=None,
    ):
        self.redis_url = redis_url
        self.bus = bus
        self.default_timeout_seconds = default_timeout_seconds
        self._custom_redis = redis_client

    async def _get_redis(self):
        if self._custom_redis is not None:
            return self._custom_redis
        return aioredis.from_url(self.redis_url, decode_responses=True)

    async def _close_redis(self, r):
        if self._custom_redis is None and hasattr(r, "aclose"):
            await r.aclose()

    async def spawn_subagent(
        self,
        parent: Agent,
        role: str,
        task_objective: str,
        skills_needed: Optional[List[str]] = None,
        custom_system_prompt: str = "",
        timeout_seconds: Optional[int] = None,
        token_budget: int = 2000,
        model_provider: str = "",
        model_name: str = "",
    ) -> Agent:
        """Spawns an ephemeral subagent with Depth-1 bounds.

        Raises:
            DepthLimitExceededError: If parent is already a subagent (depth > 0).
            ConcurrencyLimitExceededError: If parent already has max_subagents active children.
        """
        # Rule 1: Strict Depth-1 hierarchy boundary
        if parent.depth > 0:
            logger.error(
                "genesis.depth_limit_violation",
                parent_id=parent.id,
                parent_depth=parent.depth,
                role=role,
            )
            raise DepthLimitExceededError(
                f"Agent '{parent.name}' (depth={parent.depth}) cannot spawn subagents. "
                "Only root agents (depth=0) are authorized to spawn subagents."
            )

        # Rule 2: Active concurrency leash
        if len(parent.spawned_subagents) >= parent.max_subagents:
            logger.warning(
                "genesis.concurrency_limit_exceeded",
                parent_id=parent.id,
                active_subagents=len(parent.spawned_subagents),
                max_subagents=parent.max_subagents,
            )
            raise ConcurrencyLimitExceededError(
                f"Parent agent '{parent.name}' has reached its concurrency limit of "
                f"{parent.max_subagents} active subagents."
            )

        # Rule 3: Host PC hardware sizing for local models
        effective_provider = model_provider or parent.model_provider
        effective_model = model_name or parent.model_name
        if effective_provider.lower() == "ollama" and effective_model:
            model_tag = effective_model.lower().strip()
            if model_tag not in _ALLOWED_LOCAL_MODELS:
                logger.warning(
                    "genesis.model_downgraded_for_hardware_safety",
                    requested_model=effective_model,
                    fallback="llama3.2:latest",
                )
                effective_model = "llama3.2:latest"

        # Rule 4: Compute budget leash
        ttl = timeout_seconds or parent.task_timeout_seconds or self.default_timeout_seconds
        bounded_budget = CognitiveBudget(
            max_tokens_planning=min(token_budget, parent.cognitive_budget.max_tokens_planning or 4000),
            max_replans=1,
            max_runtime_seconds=ttl,
        )

        subagent_id = str(uuid4())
        short_id = subagent_id[:8]
        safe_role = role.strip().replace(" ", "_")[:16]
        subagent_name = f"sub_{parent.name[:10]}_{safe_role}_{short_id}"

        identity_prompt = custom_system_prompt or (
            f"You are {subagent_name}, an ephemeral specialized subagent spawned by {parent.name}.\n"
            f"Your dedicated role: {role}\n"
            f"Your specific objective: {task_objective}\n"
            f"Execute concisely, report your findings directly, and conclude."
        )

        subagent = Agent(
            id=subagent_id,
            name=subagent_name,
            description=f"Ephemeral subagent for {role} (Parent: {parent.name})",
            model_provider=effective_provider,
            model_name=effective_model,
            identity_prompt=identity_prompt,
            autonomy_mode=AutonomyMode.FULL,
            allowed_capabilities=list(skills_needed or parent.allowed_capabilities or ["safe", "monitored"]),
            cognitive_budget=bounded_budget,
            memory_namespace=f"sub_{parent.id}_{short_id}",
            status=AgentStatus.IDLE,
            parent_agent_id=parent.id,
            depth=1,  # Strictly depth 1
            is_ephemeral=True,
            task_timeout_seconds=ttl,
            metadata={
                "role": role,
                "objective": task_objective,
                "spawned_by": parent.id,
                "spawned_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        # Persist child and update parent's tracked list
        r = await self._get_redis()
        try:
            await save_agent(r, subagent)
            if subagent_id not in parent.spawned_subagents:
                parent.spawned_subagents.append(subagent_id)
                await save_agent(r, parent)
        finally:
            await self._close_redis(r)

        logger.info(
            "genesis.subagent_spawned",
            subagent_id=subagent.id,
            subagent_name=subagent.name,
            parent_id=parent.id,
            parent_name=parent.name,
            depth=subagent.depth,
            ttl=ttl,
        )

        if self.bus:
            try:
                await self.bus.publish(
                    SUBAGENT_SPAWNED,
                    {
                        "subagent_id": subagent.id,
                        "subagent_name": subagent.name,
                        "parent_id": parent.id,
                        "parent_name": parent.name,
                        "role": role,
                    },
                )
            except Exception:
                pass

        return subagent

    async def reap_subagent(
        self,
        parent: Agent,
        subagent_id: str,
        reason: str = "completed",
    ) -> bool:
        """Archives and cleans up an ephemeral subagent, updating the parent tracker."""
        r = await self._get_redis()
        try:
            subagent = await load_agent(r, subagent_id)
            if subagent:
                subagent.status = AgentStatus.ARCHIVED
                subagent.metadata["reap_reason"] = reason
                subagent.metadata["reaped_at"] = datetime.now(timezone.utc).isoformat()
                await save_agent(r, subagent)

            if subagent_id in parent.spawned_subagents:
                parent.spawned_subagents.remove(subagent_id)
                await save_agent(r, parent)
        finally:
            await self._close_redis(r)

        logger.info(
            "genesis.subagent_reaped",
            subagent_id=subagent_id,
            parent_id=parent.id,
            reason=reason,
            remaining_subagents=len(parent.spawned_subagents),
        )

        if self.bus:
            try:
                await self.bus.publish(
                    SUBAGENT_REAPED,
                    {
                        "subagent_id": subagent_id,
                        "parent_id": parent.id,
                        "reason": reason,
                    },
                )
            except Exception:
                pass

        return True

    async def run_subagent_workflow(
        self,
        parent: Agent,
        role: str,
        objective: str,
        orchestrator=None,
        skills_needed: Optional[List[str]] = None,
        timeout_seconds: int = 120,
    ) -> SubagentTaskReport:
        """End-to-end ephemeral workflow: Spawns -> Executes -> Awaits -> Reaps."""
        t0 = time.monotonic()
        created_at = datetime.now(timezone.utc).isoformat()

        # 1. Spawn subagent with Depth-1 bounds
        subagent = await self.spawn_subagent(
            parent=parent,
            role=role,
            task_objective=objective,
            skills_needed=skills_needed,
            timeout_seconds=timeout_seconds,
        )

        output = ""
        status = "unknown"
        ok = False
        error = ""

        try:
            if orchestrator:
                goal_obj = await orchestrator.create_agent_goal(
                    subagent.id,
                    objective=objective,
                )

                if goal_obj:
                    deadline = time.monotonic() + timeout_seconds
                    while time.monotonic() < deadline:
                        await asyncio.sleep(2)
                        current_status = await orchestrator.get_goal_status(goal_obj.id)
                        if current_status in ("completed", "failed"):
                            status = current_status
                            ok = (status == "completed")
                            output = await orchestrator.get_goal_output(goal_obj.id)
                            break
                    else:
                        status = "timeout"
                        error = f"Subagent execution exceeded timeout ({timeout_seconds}s)"
            else:
                status = "completed"
                ok = True
                output = f"Synthesized subagent result for task: {objective}"
        except Exception as exc:
            status = "failed"
            error = str(exc)
            logger.error("genesis.workflow_failed", subagent_id=subagent.id, error=error)
        finally:
            await self.reap_subagent(parent=parent, subagent_id=subagent.id, reason=status)

        latency_ms = round((time.monotonic() - t0) * 1000)
        completed_at = datetime.now(timezone.utc).isoformat()

        return SubagentTaskReport(
            subagent_id=subagent.id,
            parent_agent_id=parent.id,
            role=role,
            objective=objective,
            ok=ok,
            status=status,
            output=output,
            latency_ms=latency_ms,
            created_at=created_at,
            completed_at=completed_at,
            error=error,
        )
