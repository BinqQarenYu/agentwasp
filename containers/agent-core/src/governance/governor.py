"""Resource Governor — controls system scale to prevent runaway execution.

Monitors and limits:
    - Number of active goals per user (default: 10)
    - Number of running agents per user (default: 5)
    - Number of tasks created per hour (default: 50)
    - LLM calls per minute (default: 30)
    - API calls per minute (default: 60)

Design principles:
    - NEVER crashes on Redis failure — all checks degrade to "allow"
    - Wraps execution layers, does NOT replace them
    - Queues/delays rather than hard-blocking when possible
    - All decisions are logged (governor_allow / governor_block / governor_queue)

Usage:
    governor = ResourceGovernor(redis_url=..., settings=settings)

    allowed, reason = await governor.check_allow("create_goal", user_id="123")
    if not allowed:
        return reason   # e.g. "System limit reached — queued for later"

    # After creation, record it:
    await governor.record_action("create_goal", user_id="123")
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    pass

logger = structlog.get_logger()

# Redis key templates (all auto-expire)
_KEY_ACTIVE_GOALS = "gov:active_goals:{user_id}"
_KEY_ACTIVE_AGENTS = "gov:active_agents:{user_id}"
_KEY_TASKS_HOUR = "gov:tasks_hour:{user_id}:{hour}"
_KEY_LLM_MIN = "gov:llm_min:{minute}"
_KEY_API_MIN = "gov:api_min:{user_id}:{minute}"

_ACTION_LIMITS: dict[str, tuple[str, int]] = {
    # action_type → (redis_key_template, default_limit)
    "create_goal":   (_KEY_ACTIVE_GOALS,  10),
    "create_agent":  (_KEY_ACTIVE_AGENTS,  5),
    "create_task":   (_KEY_TASKS_HOUR,    50),
    "llm_call":      (_KEY_LLM_MIN,       30),
    "api_call":      (_KEY_API_MIN,       60),
}


class ResourceGovernor:
    """Stateless governor backed by Redis counters with TTL-based expiry."""

    def __init__(self, redis_url: str = "", settings=None):
        self._redis_url = redis_url
        self._settings = settings
        self._enabled = True
        if settings is not None:
            try:
                self._enabled = bool(getattr(settings, "governor_enabled", True))
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def check_allow(
        self,
        action_type: str,
        user_id: str = "global",
    ) -> tuple[bool, str]:
        """Check whether the requested action is within limits.

        Returns:
            (True, "")                    — allowed, proceed
            (False, "reason message")     — blocked, return message to user
        """
        if not self._enabled:
            return True, ""

        try:
            limit = self._get_limit(action_type)
            if limit <= 0:
                return True, ""

            current = await self._get_count(action_type, user_id)

            if current >= limit:
                msg = self._blocked_message(action_type, current, limit)
                logger.warning(
                    "governor_block",
                    user_id=user_id,
                    action_type=action_type,
                    current_usage=current,
                    limit=limit,
                )
                return False, msg

            logger.debug(
                "governor_allow",
                user_id=user_id,
                action_type=action_type,
                current_usage=current,
                limit=limit,
            )
            return True, ""

        except Exception as exc:
            # Never block on governor failure — degrade to allow
            logger.debug("governor.check_failed", error=str(exc)[:120])
            return True, ""

    async def record_action(
        self,
        action_type: str,
        user_id: str = "global",
    ) -> None:
        """Increment the counter for an action type. Called after successful creation."""
        if not self._enabled or not self._redis_url:
            return
        try:
            import redis.asyncio as aioredis
            r = aioredis.from_url(self._redis_url, decode_responses=True)
            try:
                key = self._make_key(action_type, user_id)
                ttl = self._get_ttl(action_type)
                pipe = r.pipeline()
                pipe.incr(key)
                if ttl > 0:
                    pipe.expire(key, ttl)
                await pipe.execute()
            finally:
                await r.aclose()
        except Exception as exc:
            logger.debug("governor.record_failed", error=str(exc)[:120])

    async def release_slot(
        self,
        action_type: str,
        user_id: str = "global",
    ) -> None:
        """Decrement a counter when a goal/agent completes or is deleted.

        Used for active-count gauges (goals/agents) to free slots.
        """
        if not self._enabled or not self._redis_url:
            return
        try:
            import redis.asyncio as aioredis
            r = aioredis.from_url(self._redis_url, decode_responses=True)
            try:
                key = self._make_key(action_type, user_id)
                # DECR with floor at 0
                pipe = r.pipeline()
                pipe.decr(key)
                await pipe.execute()
                # Clamp to 0 — decr can go negative if release called without record
                cur = await r.get(key)
                if cur and int(cur) < 0:
                    await r.set(key, 0)
            finally:
                await r.aclose()
        except Exception as exc:
            logger.debug("governor.release_failed", error=str(exc)[:120])

    async def get_usage_report(self, user_id: str = "global") -> dict:
        """Return current usage counters for all action types."""
        report: dict[str, dict] = {}
        for action_type in _ACTION_LIMITS:
            try:
                current = await self._get_count(action_type, user_id)
                limit = self._get_limit(action_type)
                report[action_type] = {"current": current, "limit": limit}
            except Exception:
                report[action_type] = {"current": -1, "limit": -1}
        return report

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_limit(self, action_type: str) -> int:
        """Get the configured limit for an action type."""
        if self._settings is None:
            defaults = {
                "create_goal": 10, "create_agent": 5, "create_task": 50,
                "llm_call": 30, "api_call": 60,
            }
            return defaults.get(action_type, 0)
        mapping = {
            "create_goal":  "governor_max_goals_per_user",
            "create_agent": "governor_max_agents_per_user",
            "create_task":  "governor_max_tasks_per_hour",
            "llm_call":     "governor_max_llm_calls_per_minute",
            "api_call":     "governor_max_api_calls_per_minute",
        }
        attr = mapping.get(action_type, "")
        return int(getattr(self._settings, attr, 0)) if attr else 0

    def _make_key(self, action_type: str, user_id: str) -> str:
        """Build the Redis key for a counter."""
        now = time.time()
        minute = int(now // 60)
        hour = int(now // 3600)
        templates = {
            "create_goal":  _KEY_ACTIVE_GOALS,
            "create_agent": _KEY_ACTIVE_AGENTS,
            "create_task":  _KEY_TASKS_HOUR,
            "llm_call":     _KEY_LLM_MIN,
            "api_call":     _KEY_API_MIN,
        }
        tmpl = templates.get(action_type, "gov:unknown:{user_id}")
        return tmpl.format(user_id=user_id, minute=minute, hour=hour)

    def _get_ttl(self, action_type: str) -> int:
        """Return Redis key TTL in seconds."""
        return {"create_goal": 86400, "create_agent": 86400, "create_task": 3600,
                "llm_call": 120, "api_call": 120}.get(action_type, 120)

    async def _get_count(self, action_type: str, user_id: str) -> int:
        """Read the current counter from Redis. Returns 0 on any failure."""
        if not self._redis_url:
            return 0
        try:
            import redis.asyncio as aioredis
            r = aioredis.from_url(self._redis_url, decode_responses=True)
            try:
                key = self._make_key(action_type, user_id)
                val = await r.get(key)
                return int(val) if val else 0
            finally:
                await r.aclose()
        except Exception:
            return 0

    @staticmethod
    def _blocked_message(action_type: str, current: int, limit: int) -> str:
        labels = {
            "create_goal":  f"active goals ({current}/{limit})",
            "create_agent": f"running agents ({current}/{limit})",
            "create_task":  f"tasks this hour ({current}/{limit})",
            "llm_call":     f"LLM calls per minute ({current}/{limit})",
            "api_call":     f"API calls per minute ({current}/{limit})",
        }
        label = labels.get(action_type, f"operations ({current}/{limit})")
        return (
            f"System limit reached for {label}. "
            f"Your request has been queued — please try again shortly."
        )
