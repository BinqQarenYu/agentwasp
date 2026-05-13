"""Goal Execution Backend abstraction — foundation for worker separation.

Introduces a thin interface that decouples scheduling responsibility from
execution responsibility. This allows future horizontal scaling without
redesigning the existing goal execution logic.

Current deployment uses LocalExecutionBackend (default, same-process).
Future deployment can swap in QueueExecutionBackend for worker isolation.

Architecture:
  agent-api       → receives requests, creates goals
  agent-scheduler → GoalTickJob decides WHICH goals need ticking
  agent-worker    → GoalExecutionBackend decides HOW ticking happens

Usage in GoalOrchestrator:
    # main.py wires the backend:
    orchestrator.execution_backend = LocalExecutionBackend()

    # GoalTickJob routes through the backend:
    await orchestrator.execution_backend.tick(orchestrator)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from .orchestrator import GoalOrchestrator

logger = structlog.get_logger()


class GoalExecutionBackend(ABC):
    """Abstract interface for goal execution dispatch.

    Implementations decide how goal ticks are routed — locally (same process)
    or via a work queue (distributed workers).
    """

    @abstractmethod
    async def tick(self, orchestrator: "GoalOrchestrator") -> str:
        """Advance all active goals by one step.

        Returns a summary string (same contract as GoalOrchestrator.tick()).
        """

    @property
    @abstractmethod
    def backend_name(self) -> str:
        """Human-readable backend identifier for logs and monitoring."""


class LocalExecutionBackend(GoalExecutionBackend):
    """Default backend — executes goals directly in the current process.

    Preserves all existing behavior. No change to goal execution semantics.
    GoalOrchestrator.tick() is called directly.
    """

    @property
    def backend_name(self) -> str:
        return "local"

    async def tick(self, orchestrator: "GoalOrchestrator") -> str:
        """Delegate directly to GoalOrchestrator.tick() — same process."""
        return await orchestrator.tick()


class QueueExecutionBackend(GoalExecutionBackend):
    """Placeholder for future distributed worker execution.

    When activated, goal ticks would be submitted to a Redis work queue
    and consumed by separate worker processes. Not yet implemented —
    falls back to LocalExecutionBackend until worker infrastructure exists.

    This stub ensures the abstraction is in place before the infrastructure
    is built, avoiding a future redesign of the goal execution path.
    """

    def __init__(self, redis_url: str = "", fallback: GoalExecutionBackend | None = None):
        self._redis_url = redis_url
        self._fallback = fallback or LocalExecutionBackend()

    @property
    def backend_name(self) -> str:
        return "queue(stub→local)"

    async def tick(self, orchestrator: "GoalOrchestrator") -> str:
        """Submit work to queue — not yet implemented, falls back to local."""
        logger.debug("execution_backend.queue_tick_fallback",
                     reason="queue worker not yet deployed, using local")
        return await self._fallback.tick(orchestrator)
