"""GoalTickJob — Scheduler-compatible callable for the Autonomous Goal Engine.

Registered with the Scheduler as a periodic job (configurable interval, default 30s).
Each invocation calls GoalOrchestrator.tick() which advances one step per active goal.
"""

from __future__ import annotations

import structlog

from .orchestrator import GoalOrchestrator

logger = structlog.get_logger()


class GoalTickJob:
    """Scheduler-compatible callable that ticks the GoalOrchestrator.

    Usage in main.py:
        scheduler.register(
            "goal_tick",
            settings.goal_tick_interval,
            GoalTickJob(orchestrator=goal_orchestrator),
        )
    """

    def __init__(self, orchestrator: GoalOrchestrator):
        self.orchestrator = orchestrator

    async def __call__(self) -> str:
        """Called by Scheduler every N seconds. Returns a status summary string.

        Routes through the orchestrator's execution_backend (LocalExecutionBackend
        by default). Swap backend to QueueExecutionBackend for worker separation.
        """
        try:
            backend = self.orchestrator.execution_backend
            summary = await backend.tick(self.orchestrator)
            logger.debug("goal_tick_job.done", summary=summary, backend=backend.backend_name)
            return summary
        except Exception as e:
            logger.exception("goal_tick_job.error")
            return f"error: {e}"
