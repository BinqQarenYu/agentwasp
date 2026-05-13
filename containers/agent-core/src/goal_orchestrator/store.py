"""Goal persistence — Redis HASH as primary store.

Storage layout:
  REDIS_KEY (HASH) = "goals"
    field: goal_id (str)
    value: JSON-serialized Goal

All reads/writes use the Goal Pydantic model for schema validation.
Callers must open and close their own Redis connection.
"""

from __future__ import annotations

import structlog

from .types import Goal, GoalState

logger = structlog.get_logger()

REDIS_KEY = "goals"

# Trim completed/failed goals beyond this count to bound memory usage
MAX_TERMINAL_GOALS = 100


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------


async def save_goal(r, goal: Goal) -> None:
    """Persist goal state to Redis HASH."""
    goal.touch()
    await r.hset(REDIS_KEY, goal.id, goal.model_dump_json())


async def load_goal(r, goal_id: str) -> Goal | None:
    """Load a single goal by ID. Returns None if not found."""
    raw = await r.hget(REDIS_KEY, goal_id)
    if not raw:
        return None
    try:
        return Goal.model_validate_json(raw)
    except Exception:
        logger.exception("goal_store.load_error", goal_id=goal_id)
        return None


async def list_goals(r) -> list[Goal]:
    """Return all goals sorted by created_at descending."""
    raw_map = await r.hgetall(REDIS_KEY)
    goals: list[Goal] = []
    for v in raw_map.values():
        try:
            goals.append(Goal.model_validate_json(v))
        except Exception:
            pass
    goals.sort(key=lambda g: g.created_at, reverse=True)
    return goals


async def list_active_goals(r) -> list[Goal]:
    """Return goals in ACTIVE state only."""
    return [g for g in await list_goals(r) if g.state == GoalState.ACTIVE]


async def delete_goal(r, goal_id: str) -> bool:
    """Hard-delete a goal from Redis. Returns True if it existed."""
    result = await r.hdel(REDIS_KEY, goal_id)
    return result > 0


async def cleanup_old_goals(r) -> int:
    """Remove oldest completed/failed goals beyond MAX_TERMINAL_GOALS.

    Keeps the most recent MAX_TERMINAL_GOALS terminal goals.
    Returns count of deleted goals.
    """
    goals = await list_goals(r)
    terminal = [
        g for g in goals if g.state in (GoalState.COMPLETED, GoalState.FAILED)
    ]
    if len(terminal) <= MAX_TERMINAL_GOALS:
        return 0
    # goals are sorted newest-first; drop the oldest
    to_delete = terminal[MAX_TERMINAL_GOALS:]
    for g in to_delete:
        await delete_goal(r, g.id)
    logger.info("goal_store.cleanup", deleted=len(to_delete))
    return len(to_delete)
