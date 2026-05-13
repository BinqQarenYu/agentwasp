"""KgInsightsUpdaterJob — periodically computes KG usage patterns.

Runs every 30 minutes. Queries the knowledge graph to find:
- Top tools/technologies the user works with (uses_tool relations)
- Most salient entities (confidence × recency)
- Total node/relation counts

Stores a JSON snapshot at Redis key ``kg:insights`` (TTL 3h) so the
opportunity engine, autonomous goal generator, and dashboard can consume
real-time KG signal without hitting the DB on every request.
"""
from __future__ import annotations

import structlog

logger = structlog.get_logger()


class KgInsightsUpdaterJob:
    """Compute and cache KG insights to Redis."""

    def __init__(self, redis_url: str = "") -> None:
        self._redis_url = redis_url

    async def __call__(self) -> str:
        try:
            from ..memory.knowledge_graph import compute_and_store_kg_insights
            insights = await compute_and_store_kg_insights(redis_url=self._redis_url)
            if not insights:
                return "kg_insights_updater: no data"
            return (
                f"kg_insights_updater: nodes={insights.get('node_count', 0)} "
                f"rels={insights.get('rel_count', 0)} "
                f"top_tools={insights.get('top_tools', [])[:3]}"
            )
        except Exception as exc:
            logger.exception("kg_insights_updater.failed", error=str(exc))
            return f"kg_insights_updater: failed — {exc}"
