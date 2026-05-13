"""Goal Event Sourcing — append-only event log using Redis Streams.

Each goal gets its own stream: goal:events:{goal_id}
Events are appended on every meaningful state transition.
The stream is the source of truth for auditability and replay.

Supported events (matching spec):
  GoalCreated    — goal first saved (state=PLANNING)
  GoalStarted    — planning complete, state→ACTIVE
  TaskStarted    — a task begins execution
  TaskCompleted  — a task finishes (success or fail)
  GoalCompleted  — state→COMPLETED
  GoalFailed     — state→FAILED
  GoalCancelled  — cancelled via cancel_goal()

Usage (fire-and-forget):
    import asyncio
    from .events import emit_goal_event
    asyncio.get_running_loop().create_task(
        emit_goal_event(redis_url, "GoalCreated", goal_id=..., state="planning", ...)
    )

Replay:
    history = await get_goal_events(redis_url, goal_id)
    replayed = replay_goal_state(history)
    issues   = await validate_goal_consistency(redis_url, goal)
"""
from __future__ import annotations

import structlog
from datetime import datetime, timezone

logger = structlog.get_logger()

_STREAM_PREFIX = "goal:events:"
_STREAM_TTL_SECONDS = 7 * 86400  # 7 days


def _stream_key(goal_id: str) -> str:
    return f"{_STREAM_PREFIX}{goal_id}"


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


async def emit_goal_event(
    redis_url: str,
    event_type: str,
    goal_id: str,
    **fields: str,
) -> None:
    """Append a goal lifecycle event to the per-goal Redis Stream.

    Fire-and-forget — creates its own Redis connection, never raises.
    """
    import redis.asyncio as aioredis
    try:
        r = aioredis.from_url(redis_url, decode_responses=True)
        try:
            stream = _stream_key(goal_id)
            data = {
                "event": event_type,
                "goal_id": goal_id,
                "ts": datetime.now(timezone.utc).isoformat(),
                **{k: str(v) for k, v in fields.items()},
            }
            await r.xadd(stream, data)
            # Set TTL only on the very first entry (EXPIRE returns 1 on first set)
            await r.expire(stream, _STREAM_TTL_SECONDS)
            logger.debug(
                f"goal_event.{event_type.lower()}",
                goal_id=goal_id[:8],
                **{k: str(v)[:60] for k, v in fields.items()},
            )
        finally:
            await r.aclose()
    except Exception as exc:
        # Event sourcing is best-effort — never block the goal lifecycle
        logger.debug("goal_events.emit_error", event_type=event_type, goal_id=goal_id[:8], error=str(exc)[:120])


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


async def get_goal_events(redis_url: str, goal_id: str) -> list[dict]:
    """Read all events from a goal's Redis Stream. Returns [] on any error."""
    import redis.asyncio as aioredis
    try:
        r = aioredis.from_url(redis_url, decode_responses=True)
        try:
            stream = _stream_key(goal_id)
            entries = await r.xrange(stream, "-", "+")
            return [fields for _entry_id, fields in entries]
        finally:
            await r.aclose()
    except Exception as exc:
        logger.debug("goal_events.read_error", goal_id=goal_id[:8], error=str(exc)[:120])
        return []


def replay_goal_state(events: list[dict]) -> dict:
    """Reconstruct goal state from an ordered event list.

    Returns a summary dict:
      {
        "goal_id": str,
        "state": str,          # last known state from events
        "event_count": int,
        "task_outcomes": dict, # task_id → "completed"|"failed"
        "last_event": str,
        "last_ts": str,
      }
    """
    if not events:
        return {"goal_id": "", "state": "unknown", "event_count": 0,
                "task_outcomes": {}, "last_event": "", "last_ts": ""}

    # Map event types to states
    _event_to_state = {
        "GoalCreated":   "planning",
        "GoalStarted":   "active",
        "GoalCompleted": "completed",
        "GoalFailed":    "failed",
        "GoalCancelled": "failed",
    }

    state = "planning"
    task_outcomes: dict[str, str] = {}
    goal_id = events[0].get("goal_id", "")
    last_ts = ""
    last_event = ""

    for ev in events:
        ev_type = ev.get("event", "")
        last_event = ev_type
        last_ts = ev.get("ts", "")

        if ev_type in _event_to_state:
            state = _event_to_state[ev_type]

        if ev_type == "TaskCompleted":
            tid = ev.get("task_id", "unknown")
            task_outcomes[tid] = ev.get("task_status", "done")

    return {
        "goal_id": goal_id,
        "state": state,
        "event_count": len(events),
        "task_outcomes": task_outcomes,
        "last_event": last_event,
        "last_ts": last_ts,
    }


# ---------------------------------------------------------------------------
# Consistency validation
# ---------------------------------------------------------------------------


async def validate_goal_consistency(redis_url: str, goal) -> list[str]:
    """Compare snapshot state vs replayed event state. Log mismatches.

    Returns a list of issue strings (empty = consistent).
    """
    events = await get_goal_events(redis_url, goal.id)
    if not events:
        # No events yet — could be a goal created before event sourcing was added
        return []

    replayed = replay_goal_state(events)
    issues = []

    snapshot_state = goal.state.value.lower()
    replayed_state = replayed["state"].lower()

    if snapshot_state != replayed_state:
        msg = (
            f"Goal {goal.id[:8]} state mismatch: "
            f"snapshot={snapshot_state} replayed={replayed_state} "
            f"(events={replayed['event_count']})"
        )
        logger.warning("goal_events.consistency_mismatch", **{
            "goal_id": goal.id[:8],
            "snapshot_state": snapshot_state,
            "replayed_state": replayed_state,
            "event_count": replayed["event_count"],
        })
        issues.append(msg)

    return issues
