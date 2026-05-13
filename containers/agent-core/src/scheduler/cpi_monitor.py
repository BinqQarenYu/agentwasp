"""Cognitive Pressure Index Monitor — scheduler job.

Runs every 5 minutes. Computes CPI and updates Redis flags.
High-CPI actuators: autonomous, dream, perception jobs skip if agent:cpi_high is set.
"""
from __future__ import annotations

import structlog
from ..agent.cpi import compute_and_store

logger = structlog.get_logger()


class CognitiveLoadMonitorJob:
    """Computes CPI every tick and sets/clears the cpi_high Redis flag."""

    def __init__(self, redis_url: str, max_concurrent_goals: int = 3) -> None:
        self.redis_url = redis_url
        self.max_concurrent_goals = max_concurrent_goals

    async def __call__(self) -> str:
        try:
            report = await compute_and_store(
                self.redis_url,
                max_concurrent_goals=self.max_concurrent_goals,
            )
            status = "HIGH" if report.get("high") else "ok"
            return f"cpi={report['cpi']:.1f} [{status}]"
        except Exception:
            logger.exception("cpi_monitor.error")
            return "cpi_monitor: error"
