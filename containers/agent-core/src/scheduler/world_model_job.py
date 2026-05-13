"""WorldModelUpdateJob — scheduler job for System 4 (World Model).

Updates entity states every 15 minutes from world_timeline observations.
Runs when WORLD_MODEL_ENABLED=true (default).
"""

from __future__ import annotations

import time

import structlog

logger = structlog.get_logger()


class WorldModelUpdateJob:
    """Updates the WorldModel entity state table from world_timeline."""

    def __init__(self, ollama_url: str = ""):
        self._ollama_url = ollama_url

    async def __call__(self) -> str:
        from ..db.session import async_session
        from ..world.world_model import WorldModel

        t0 = time.monotonic()
        wm = WorldModel(ollama_url=self._ollama_url)

        try:
            async with async_session() as session:
                updated = await wm.update_all_entities(session)
        except Exception as exc:
            logger.warning("world_model_job.error", error=str(exc)[:120])
            updated = 0

        latency_ms = round((time.monotonic() - t0) * 1000)
        result = f"World model: {updated} entities updated ({latency_ms}ms)"
        logger.info("world_model_job.done", updated=updated, latency_ms=latency_ms)
        return result
