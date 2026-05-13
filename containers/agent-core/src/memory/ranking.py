"""Memory Ranking System — composite relevance scoring for memory injection.

Ranks retrieved memories by a weighted formula before selecting top-N for
injection into the LLM context. Prevents stale or irrelevant memories from
polluting the prompt.

Score formula:
    score = 0.5 * similarity + 0.3 * recency + 0.2 * importance

Where:
    similarity  — semantic closeness to query (0.0–1.0); defaults to 0.5 if unknown
    recency     — exponential decay based on age: exp(-age_hours / half_life)
    importance  — explicit importance tag (0.0–1.0); defaults to 0.5 if unknown

All failures are silent — ranking falls back to original order.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

import structlog

logger = structlog.get_logger()

_DEFAULT_SIMILARITY = 0.5
_DEFAULT_IMPORTANCE = 0.5
_DEFAULT_HALF_LIFE_HOURS = 24.0


def _recency_score(created_at: datetime | str | None, half_life_hours: float) -> float:
    """Exponential recency score: 1.0 for brand-new, decays with age."""
    if created_at is None:
        return _DEFAULT_SIMILARITY
    try:
        if isinstance(created_at, str):
            dt = datetime.fromisoformat(created_at)
        else:
            dt = created_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
        return math.exp(-age_hours / max(half_life_hours, 1.0))
    except Exception:
        return _DEFAULT_SIMILARITY


def _get_field(item: dict, *keys: str, default: float = 0.5) -> float:
    """Extract a float field from a dict, trying multiple key names."""
    for key in keys:
        val = item.get(key)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                pass
    return default


def rank_memories(
    memories: list[dict],
    half_life_hours: float = _DEFAULT_HALF_LIFE_HOURS,
) -> list[dict]:
    """Rank a list of memory dicts by composite score (descending).

    Each dict may contain:
        score / similarity  — float 0-1 (from vector search)
        created_at          — ISO datetime string or datetime object
        importance          — float 0-1 (explicit importance tag)

    Returns a new sorted list with 'rank_score' added to each item.
    Falls back to original order on any exception.
    """
    if not memories:
        return memories
    try:
        scored: list[tuple[float, dict]] = []
        for item in memories:
            similarity = _get_field(item, "score", "similarity", default=_DEFAULT_SIMILARITY)
            importance = _get_field(item, "importance", default=_DEFAULT_IMPORTANCE)
            created_at = item.get("created_at") or item.get("timestamp")
            recency = _recency_score(created_at, half_life_hours)

            composite = 0.5 * similarity + 0.3 * recency + 0.2 * importance
            composite = max(0.0, min(1.0, composite))

            ranked_item = {**item, "rank_score": round(composite, 4)}
            scored.append((composite, ranked_item))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in scored]

    except Exception as exc:
        logger.debug("memory_ranking.failed", error=str(exc)[:120])
        return memories


def rank_and_cap(
    memories: list[dict],
    limit: int,
    half_life_hours: float = _DEFAULT_HALF_LIFE_HOURS,
    memory_type: str = "unknown",
    goal_id: str = "",
) -> list[dict]:
    """Rank memories and return top-N.

    Emits a memory_ranked log event for observability (Part 6).
    """
    ranked = rank_memories(memories, half_life_hours)
    selected = ranked[:limit]

    if selected:
        logger.info(
            "memory_ranked",
            memory_type=memory_type,
            goal_id=goal_id or None,
            total_candidates=len(memories),
            selected=len(selected),
            top_score=selected[0].get("rank_score", 0) if selected else 0,
        )

    return selected
