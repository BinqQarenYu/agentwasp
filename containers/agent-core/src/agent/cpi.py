"""Cognitive Pressure Index (CPI) — composite load metric for Agent Wasp.

CPI is a value in [0, 100] that reflects how cognitively stressed the agent is.
High CPI means the agent should throttle autonomous/background activity to
avoid overloading the system or degrading user-facing response quality.

Components (each 0–100, weighted):
  - active_goals_pressure  (20%): # of ACTIVE goals vs max_concurrent
  - error_rate_pressure    (25%): recent errors in audit_log / total events
  - latency_pressure       (20%): recent avg latency_ms vs healthy baseline
  - memory_growth_pressure (15%): episodic memory entries in last hour
  - cpu_pressure           (20%): CPU usage % via psutil

Actuators:
  - CPI > 80 → set Redis flag ``agent:cpi_high`` (TTL 10 min)
  - CPI ≤ 60 → clear the flag
  - Dream, Autonomous, Perception jobs read this flag and skip if set.

Redis keys:
  - ``agent:cpi``      → JSON with breakdown + timestamp (TTL 10 min)
  - ``agent:cpi_high`` → "1" when CPI > 80 (TTL 10 min, refreshed each tick)
"""
from __future__ import annotations

import asyncio
import json
import structlog
from datetime import datetime, timezone

import redis.asyncio as aioredis

logger = structlog.get_logger()

CPI_KEY = "agent:cpi"
CPI_HIGH_KEY = "agent:cpi_high"
CPI_HIGH_THRESHOLD = 80.0
CPI_CLEAR_THRESHOLD = 60.0
CPI_TTL_SECONDS = 600  # 10 minutes

# Component weights (must sum to 1.0)
_WEIGHTS = {
    "active_goals":   0.20,
    "error_rate":     0.25,
    "latency":        0.20,
    "memory_growth":  0.15,
    "cpu":            0.20,
}

# Latency baseline: requests below this (ms) are "healthy"
_LATENCY_HEALTHY_MS = 2000.0
_LATENCY_CRITICAL_MS = 15000.0

# Memory growth: entries in last 1h above this → pressure
_MEMORY_GROWTH_CRITICAL = 200


async def compute_and_store(
    redis_url: str,
    max_concurrent_goals: int = 3,
) -> dict:
    """Compute CPI, store in Redis, set/clear cpi_high flag.

    Returns the full CPI dict for logging/display.
    """
    breakdown = await _gather_components(redis_url, max_concurrent_goals)
    cpi_value = _weighted_sum(breakdown)
    # Hard override: sustained high CPU (≥85%) always signals high pressure
    # regardless of weighted score — prevents local LLM inference from hiding it.
    _cpu_raw = breakdown.get("cpu", 0.0)
    _force_high = _cpu_raw >= 85.0
    _is_high = _force_high or cpi_value > CPI_HIGH_THRESHOLD

    report = {
        "cpi": round(cpi_value, 1),
        "high": _is_high,
        "components": breakdown,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }

    r = aioredis.from_url(redis_url, decode_responses=True)
    try:
        await r.setex(CPI_KEY, CPI_TTL_SECONDS, json.dumps(report))
        if _is_high:
            await r.setex(CPI_HIGH_KEY, CPI_TTL_SECONDS, "1")
            logger.warning(
                "cpi.high",
                cpi=round(cpi_value, 1),
                cpu_raw=round(_cpu_raw, 1),
                forced=_force_high,
                breakdown={k: round(v, 1) for k, v in breakdown.items()},
            )
        elif cpi_value <= CPI_CLEAR_THRESHOLD and not _force_high:
            await r.delete(CPI_HIGH_KEY)
        # else: keep previous flag until it naturally expires (TTL)
    finally:
        await r.aclose()

    return report


async def is_high(redis_url: str) -> bool:
    """Return True if CPI_HIGH flag or saccadic CPU-high flag is currently set.

    The saccadic vision thread sets ``agent:saccadic_cpu_high`` within 2 seconds of
    a CPU spike, giving much faster backpressure than the 5-minute CPI job cycle.
    """
    try:
        r = aioredis.from_url(redis_url, decode_responses=True)
        cpi_val, saccadic_val = await asyncio.gather(
            r.get(CPI_HIGH_KEY),
            r.get("agent:saccadic_cpu_high"),
        )
        await r.aclose()
        return cpi_val == "1" or saccadic_val == "1"
    except Exception:
        return False  # On error, assume not high (fail-open)


async def load(redis_url: str) -> dict:
    """Load the last computed CPI report from Redis."""
    try:
        r = aioredis.from_url(redis_url, decode_responses=True)
        raw = await r.get(CPI_KEY)
        await r.aclose()
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------------------
# Component gatherers
# ---------------------------------------------------------------------------

async def _gather_components(redis_url: str, max_concurrent_goals: int) -> dict[str, float]:
    cpu = _cpu_pressure()
    active_goals = await _active_goals_pressure(redis_url, max_concurrent_goals)
    error_rate = await _error_rate_pressure(redis_url)
    latency = await _latency_pressure(redis_url)
    memory_growth = await _memory_growth_pressure(redis_url)
    return {
        "active_goals":  round(active_goals, 1),
        "error_rate":    round(error_rate, 1),
        "latency":       round(latency, 1),
        "memory_growth": round(memory_growth, 1),
        "cpu":           round(cpu, 1),
    }


def _weighted_sum(breakdown: dict[str, float]) -> float:
    total = 0.0
    for key, weight in _WEIGHTS.items():
        total += breakdown.get(key, 0.0) * weight
    return min(100.0, max(0.0, total))


def _cpu_pressure() -> float:
    try:
        import psutil
        return min(100.0, psutil.cpu_percent(interval=0.2))
    except Exception:
        return 0.0


async def _active_goals_pressure(redis_url: str, max_concurrent: int) -> float:
    try:
        r = aioredis.from_url(redis_url, decode_responses=True)
        goals_raw = await r.hgetall("goals")
        await r.aclose()
        active = sum(
            1 for v in goals_raw.values()
            if json.loads(v).get("state") == "active"
        )
        if max_concurrent <= 0:
            return 0.0
        return min(100.0, active / max_concurrent * 100.0)
    except Exception:
        return 0.0


async def _error_rate_pressure(redis_url: str) -> float:
    """Use audit_log recent error rate (last 30 min)."""
    try:
        from ..db.session import async_session
        from sqlalchemy import text as sql_text
        async with async_session() as session:
            result = await session.execute(sql_text("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(CASE WHEN error IS NOT NULL THEN 1 END) AS errors
                FROM audit_log
                WHERE timestamp > NOW() - INTERVAL '30 minutes'
            """))
            row = result.fetchone()
            if row and row[0] and row[0] > 0:
                return min(100.0, (row[1] / row[0]) * 100.0)
    except Exception:
        pass
    return 0.0


async def _latency_pressure(redis_url: str) -> float:
    """Average recent latency vs healthy baseline."""
    try:
        from ..db.session import async_session
        from sqlalchemy import text as sql_text
        async with async_session() as session:
            result = await session.execute(sql_text("""
                SELECT AVG(latency_ms)
                FROM audit_log
                WHERE timestamp > NOW() - INTERVAL '30 minutes'
                  AND latency_ms > 0
            """))
            avg = result.scalar()
            if avg is None:
                return 0.0
            # Linear scale: 0 at _LATENCY_HEALTHY_MS, 100 at _LATENCY_CRITICAL_MS
            pressure = (avg - _LATENCY_HEALTHY_MS) / (_LATENCY_CRITICAL_MS - _LATENCY_HEALTHY_MS) * 100.0
            return min(100.0, max(0.0, pressure))
    except Exception:
        return 0.0


async def _memory_growth_pressure(redis_url: str) -> float:
    """Count new episodic memory entries in the last hour."""
    try:
        from ..db.session import async_session
        from sqlalchemy import text as sql_text
        async with async_session() as session:
            result = await session.execute(sql_text("""
                SELECT COUNT(*)
                FROM memory_entries
                WHERE memory_type = 'episodic'
                  AND created_at > NOW() - INTERVAL '1 hour'
            """))
            count = result.scalar() or 0
            return min(100.0, count / _MEMORY_GROWTH_CRITICAL * 100.0)
    except Exception:
        return 0.0
