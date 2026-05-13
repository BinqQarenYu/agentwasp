"""CapabilityEvolutionJob — periodic scan for accumulated capability gaps.

Runs every 3600s (configurable). Checks recent failed goal reflections
for gap signals and triggers CapabilityEvolutionEngine.analyze_gap()
when thresholds are met.

This is a companion to the fire-and-forget hook in GoalOrchestrator._tick_one().
The periodic scan catches gaps that may have accumulated across multiple
short-lived goal failures that individually didn't meet the threshold.
"""
from __future__ import annotations

import structlog

logger = structlog.get_logger()


class CapabilityEvolutionJob:
    """Thin scheduler wrapper for CapabilityEvolutionEngine periodic gap scan."""

    def __init__(self, engine) -> None:
        self.engine = engine

    async def __call__(self) -> None:
        """Scan recent high-importance failure reflections for capability gaps."""
        if not self.engine:
            return
        try:
            if not self.engine.reflection_engine:
                return

            # Fetch recent failure reflections with high importance (well-documented failures)
            recent = await self.engine.reflection_engine.get_recent_reflections(limit=5)
            candidates = [
                r for r in recent
                if r.get("outcome") == "failure" and r.get("importance", 0) >= 0.7
            ]

            evolved = 0
            for ref in candidates:
                goal_id = ref.get("goal_id", "")
                reflection_text = ref.get("reflection", "")
                if not goal_id or not reflection_text:
                    continue

                # Use reflection text as the "objective" signal since we may
                # not have the original goal objective in the scheduler context.
                result = await self.engine.analyze_gap(
                    goal_id=goal_id,
                    objective=reflection_text,
                    error="",
                    outcome="failure",
                    consecutive_failures=2,   # Periodic scan assumes at least 2 failures
                )
                if result:
                    evolved += 1

            if evolved:
                logger.info("cee_job.evolutions_triggered", count=evolved)

        except Exception as exc:
            logger.debug("cee_job.error", error=str(exc)[:120])
