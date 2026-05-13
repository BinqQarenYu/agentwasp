"""Goal-Specific Memory — observations scoped to a single active goal.

Each GoalMemory entry is tied to a goal_id. Only retrieved when the caller
provides a matching goal_id, preventing cross-goal memory pollution.

Table: goal_memory (auto-created by SQLAlchemy create_all)
  - goal_id    : str  — matches Goal.id in the goal orchestrator
  - observation: str  — what was observed during this goal's execution
  - importance : float 0-1 — how important the observation is
  - created_at : datetime

Usage:
    # From within a goal step execution:
    await add_observation(goal_id="abc123", observation="BTC jumped 3%", importance=0.8)

    # From build_context():
    obs = await get_observations(goal_id="abc123", limit=5)
    block = format_for_context(obs)
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import structlog

logger = structlog.get_logger()


async def add_observation(
    goal_id: str,
    observation: str,
    importance: float = 0.5,
) -> bool:
    """Store a goal-scoped observation. Returns True on success, False on failure."""
    if not goal_id or not observation:
        return False
    try:
        from ..db.session import async_session
        from ..db.models import GoalMemory
        async with async_session() as session:
            entry = GoalMemory(
                id=str(uuid4()),
                goal_id=goal_id,
                observation=observation[:1000],
                importance=max(0.0, min(1.0, importance)),
                created_at=datetime.now(timezone.utc),
            )
            session.add(entry)
            await session.commit()
        logger.info(
            "goal_memory_added",
            goal_id=goal_id,
            observation_preview=observation[:80],
            importance=importance,
        )
        return True
    except Exception as exc:
        logger.debug("goal_memory.add_failed", error=str(exc)[:120])
        return False


async def get_observations(goal_id: str, limit: int = 5) -> list[dict]:
    """Retrieve goal-scoped observations ordered by importance then recency.

    Returns list of dicts: {observation, importance, created_at, rank_score}.
    Returns [] on any failure — never raises.
    """
    if not goal_id:
        return []
    try:
        from sqlalchemy import select
        from ..db.session import async_session
        from ..db.models import GoalMemory
        async with async_session() as session:
            result = await session.execute(
                select(GoalMemory)
                .where(GoalMemory.goal_id == goal_id)
                .order_by(GoalMemory.importance.desc(), GoalMemory.created_at.desc())
                .limit(limit * 3)  # over-fetch for ranking
            )
            rows = result.scalars().all()

        if not rows:
            return []

        memories = [
            {
                "observation": r.observation,
                "importance": r.importance,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "score": r.importance,  # for ranking system compatibility
            }
            for r in rows
        ]

        # Apply ranking before capping
        from .ranking import rank_and_cap
        ranked = rank_and_cap(memories, limit=limit, memory_type="goal", goal_id=goal_id)

        logger.info(
            "goal_memory_used",
            goal_id=goal_id,
            retrieved=len(ranked),
            memory_type="goal",
            memory_score=ranked[0].get("rank_score", 0) if ranked else 0,
        )
        return ranked
    except Exception as exc:
        logger.debug("goal_memory.get_failed", error=str(exc)[:120])
        return []


def format_for_context(observations: list[dict], goal_id: str = "") -> str:
    """Format goal observations as a context block for injection."""
    if not observations:
        return ""
    lines = [f"[GOAL MEMORY — observations for active goal {goal_id or 'current'}:]"]
    for i, obs in enumerate(observations, 1):
        imp = obs.get("importance", 0.5)
        imp_label = "HIGH" if imp >= 0.8 else ("MED" if imp >= 0.5 else "LOW")
        lines.append(f"  {i}. [{imp_label}] {obs['observation']}")
    return "\n".join(lines)
