"""SkillEvolutionJob — scheduler job for System 5 (Skill Evolution Engine).

Runs every 6 hours when SKILL_EVOLUTION_ENABLED=true.
Analyzes audit_log for recurring skill patterns and synthesises composite skills.
"""

from __future__ import annotations

import time

import structlog

logger = structlog.get_logger()


class SkillEvolutionJob:
    """Detects and synthesises new composite skills from usage patterns."""

    def __init__(self, model_manager, min_pattern_count: int = 5):
        self._model_manager = model_manager
        self._min_pattern_count = min_pattern_count

    async def __call__(self) -> str:
        from ..db.session import async_session
        from ..skills.skill_evolution import SkillEvolutionEngine

        t0 = time.monotonic()
        engine = SkillEvolutionEngine(
            model_manager=self._model_manager,
            min_pattern_count=self._min_pattern_count,
        )

        try:
            async with async_session() as session:
                result = await engine.run(session)
        except Exception as exc:
            logger.warning("skill_evolution_job.error", error=str(exc)[:120])
            result = {"patterns_detected": 0, "skills_synthesised": 0}

        latency_ms = round((time.monotonic() - t0) * 1000)
        summary = (
            f"Skill evolution: {result.get('patterns_detected', 0)} patterns, "
            f"{result.get('skills_synthesised', 0)} synthesised ({latency_ms}ms)"
        )
        return summary
