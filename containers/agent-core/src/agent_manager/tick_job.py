"""AgentTickJob — scheduler job that ticks all active agent runtimes."""

from __future__ import annotations

import structlog

from .orchestrator import AgentOrchestrator

logger = structlog.get_logger()


class AgentTickJob:
    """Registered as 'agent_tick' in the Scheduler.

    Calls AgentOrchestrator.tick() which runs one step per active agent,
    subject to CPU backpressure and global token budget controls.
    """

    def __init__(self, orchestrator: AgentOrchestrator) -> None:
        self.orchestrator = orchestrator

    async def __call__(self) -> str:
        try:
            await self.orchestrator.tick()
            return "ok"
        except Exception:
            logger.exception("agent_tick_job.error")
            return "error"
