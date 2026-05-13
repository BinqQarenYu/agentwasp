"""Adaptive execution health state — WASP v2.5.

Lightweight system health snapshot used to select execution strategy.
Reads from the existing CPI Redis key (``agent:cpi``) when available;
falls back to psutil stubs if Redis is unreachable or the key is missing.

This module is **purely additive** — no existing code path depends on it.
When ``health_state`` is absent from ctx, all behaviour is unchanged.

Usage
-----
    from src.runtime.health_state import evaluate_health_from_redis, should_use_light_mode

    health = await evaluate_health_from_redis(redis_url)
    ctx.health_state = health

    if should_use_light_mode(ctx):
        # downgrade to lighter tool; never hard-block execution
        ...
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────
_CPU_THRESHOLD     = 80.0   # % — above this → light mode
_MEMORY_THRESHOLD  = 80.0   # % — above this → light mode
_LATENCY_THRESHOLD = 500.0  # ms — above this → light mode


@dataclass
class HealthState:
    """Point-in-time system health snapshot.

    Attributes
    ----------
    cpu_percent:    Raw CPU usage (0-100), from psutil or CPI cache.
    memory_percent: Raw virtual-memory usage (0-100), from psutil.
    latency_ms:     Approximate recent avg request latency in ms.
    mode:           "full" — normal execution.
                    "light" — system under load; prefer cheaper tools.
    """
    cpu_percent: float
    memory_percent: float
    latency_ms: float
    mode: str  # "full" | "light"


def evaluate_health(cpu: float, memory: float, latency: float) -> HealthState:
    """Classify raw metrics into a HealthState.

    mode = "light" when:
        cpu > 80  OR  memory > 80  OR  latency > 500

    Safe to call with stub values (0.0) — always returns a valid HealthState.
    """
    mode = (
        "light"
        if (cpu > _CPU_THRESHOLD or memory > _MEMORY_THRESHOLD or latency > _LATENCY_THRESHOLD)
        else "full"
    )
    logger.info(
        "health_mode_selected mode=%r cpu=%.1f memory=%.1f latency=%.1f",
        mode, cpu, memory, latency,
    )
    return HealthState(
        cpu_percent=cpu,
        memory_percent=memory,
        latency_ms=latency,
        mode=mode,
    )


async def evaluate_health_from_redis(redis_url: str) -> HealthState:
    """Build a HealthState from existing telemetry in Redis.

    Reads ``agent:cpi`` (written by CognitiveLoadMonitorJob every 5 min).
    The CPI components contain:
      - ``cpu``: raw psutil CPU % (maps directly)
      - ``latency``: 0-100 pressure score (scaled back to approximate ms)

    Memory % is read from psutil directly (not stored in CPI).

    On any failure (Redis down, key missing, import error) returns safe
    stub values → full mode.  Never raises.
    """
    cpu = 0.0
    memory = 0.0
    latency = 0.0

    try:
        import json
        import redis.asyncio as aioredis

        r = aioredis.from_url(redis_url, decode_responses=True)
        try:
            raw = await r.get("agent:cpi")
        finally:
            await r.aclose()

        if raw:
            data = json.loads(raw)
            components = data.get("components", {})
            # cpu component is raw psutil % (see agent/cpi.py _cpu_pressure())
            cpu = float(components.get("cpu", 0.0))
            # latency component is a 0-100 pressure score.
            # CPI: healthy=2000ms, critical=15000ms → pressure 0-100.
            # Rough inverse: pressure 3.3 ≈ 500ms.  Scale factor: ×15.
            _lat_pressure = float(components.get("latency", 0.0))
            latency = _lat_pressure * 15.0

    except Exception as _exc:
        logger.debug("health_state.redis_miss error=%r", str(_exc)[:80])

    # Memory % from psutil — not in CPI, read directly (synchronous, fast)
    try:
        import psutil
        memory = psutil.virtual_memory().percent
    except Exception:
        pass

    return evaluate_health(cpu, memory, latency)


def should_use_light_mode(ctx: object) -> bool:
    """Return True when ctx carries a 'light' HealthState.

    Safe to call on any object — returns False when health_state is absent
    or None.  No effect on behaviour when health monitoring is not wired in.
    """
    hs = getattr(ctx, "health_state", None)
    return hs is not None and hs.mode == "light"
