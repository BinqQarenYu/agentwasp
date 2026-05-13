"""Cognitive Impact Tracker — records which cognitive systems influenced each decision.

Stores a rolling log of the last 1000 decisions in Redis list ``cognitive:impact_log``.
Each entry records:
  - decision_source: which cognitive block was active (procedural/epistemic/kg/behavioral/etc.)
  - action_taken:    short description of what the agent did
  - outcome:         "success" | "failure" | "unknown"
  - chat_id:         conversation context
  - timestamp:       ISO-8601

This enables real evaluation of which systems are causally affecting behavior.
The dashboard cognitive page reads this log for the Impact Trail panel.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal

import structlog

logger = structlog.get_logger()

IMPACT_LOG_KEY = "cognitive:impact_log"
IMPACT_LOG_MAXLEN = 1000

OutcomeType = Literal["success", "failure", "unknown"]


async def record_impact(
    redis_url: str,
    decision_sources: list[str],
    action_taken: str,
    outcome: OutcomeType = "unknown",
    chat_id: str = "",
) -> None:
    """Append a cognitive impact record to the rolling log.

    Fire-and-forget safe — all exceptions silently caught.

    Args:
        redis_url:        Redis connection string
        decision_sources: List of cognitive system names that were active,
                          e.g. ["procedural", "epistemic", "kg"]
        action_taken:     Short description of the agent's action (<=200 chars)
        outcome:          Whether the action succeeded
        chat_id:          Conversation identifier
    """
    if not redis_url:
        return
    try:
        import redis.asyncio as aioredis
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "decision_sources": decision_sources,
            "action_taken": action_taken[:200],
            "outcome": outcome,
            "chat_id": chat_id,
        }
        r = aioredis.from_url(redis_url, decode_responses=True)
        try:
            pipe = r.pipeline()
            pipe.lpush(IMPACT_LOG_KEY, json.dumps(entry))
            pipe.ltrim(IMPACT_LOG_KEY, 0, IMPACT_LOG_MAXLEN - 1)
            await pipe.execute()
        finally:
            await r.aclose()
    except Exception:
        pass


async def get_recent_impacts(redis_url: str, count: int = 50) -> list[dict]:
    """Return the most recent N impact records."""
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(redis_url, decode_responses=True)
        try:
            raw_entries = await r.lrange(IMPACT_LOG_KEY, 0, count - 1)
            return [json.loads(e) for e in raw_entries]
        finally:
            await r.aclose()
    except Exception:
        return []


async def get_impact_stats(redis_url: str) -> dict:
    """Return aggregate stats: which sources appear most, success rates."""
    entries = await get_recent_impacts(redis_url, count=200)
    if not entries:
        return {}

    source_counts: dict[str, int] = {}
    outcome_counts: dict[str, int] = {"success": 0, "failure": 0, "unknown": 0}

    for entry in entries:
        for src in entry.get("decision_sources", []):
            source_counts[src] = source_counts.get(src, 0) + 1
        outcome = entry.get("outcome", "unknown")
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1

    total = len(entries)
    success_rate = outcome_counts["success"] / total if total else 0.0

    return {
        "total_recorded": total,
        "source_frequency": dict(sorted(source_counts.items(), key=lambda x: -x[1])),
        "outcome_counts": outcome_counts,
        "success_rate": round(success_rate, 3),
    }
