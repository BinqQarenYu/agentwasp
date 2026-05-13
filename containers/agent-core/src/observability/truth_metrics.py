"""Phase 3 closure — truth/security/scheduler metrics counters.

Best-effort Redis-hash counters bumped at the moment a guard intervenes
or a security violation is detected. Telemetry never raises; if Redis is
unavailable the counter is silently dropped.

Read via dashboard route ``/metrics/api/truth``.

Counter keys:
    truth:metrics:total                    — single hash, all-time
    truth:metrics:day:YYYY-MM-DD           — daily, TTL 60d

Fields (per hash):
    honesty_layer_applied
    honesty_layer_v2_attribute_override
    honesty_layer_v2_capability_override
    honesty_layer_v2_ungrounded_data
    honesty_layer_v2_memory_fabrication
    honesty_layer_v2_passthrough          (does NOT increment — saves space)
    python_exec_security_violation
    url_substitution_blocked
    fetch_url_ssrf_blocked
    task_circuit_breaker_tripped
    task_failing_warning_sent
    task_outcome_completed
    task_outcome_timeout
    task_outcome_exception
"""
from __future__ import annotations

import datetime as _dt

import structlog

logger = structlog.get_logger()

_TOTAL_KEY = "truth:metrics:total"
_DAY_TTL = 60 * 60 * 24 * 60  # 60 days


async def bump(redis_url: str | None, field: str, by: int = 1) -> None:
    """Best-effort metric increment. Never raises."""
    if not redis_url:
        return
    try:
        import redis.asyncio as aioredis
        day_key = f"truth:metrics:day:{_dt.date.today().isoformat()}"
        r = aioredis.from_url(redis_url, decode_responses=True)
        try:
            pipe = r.pipeline()
            pipe.hincrby(_TOTAL_KEY, field, by)
            pipe.hincrby(day_key, field, by)
            pipe.expire(day_key, _DAY_TTL)
            await pipe.execute()
        finally:
            await r.aclose()
    except Exception:
        # Telemetry failure must never break a publish.
        pass
