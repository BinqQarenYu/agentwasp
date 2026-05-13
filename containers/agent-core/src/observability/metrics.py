"""Lightweight metrics collector for WASP agent observability.

Design principles:
- In-memory ring buffer (no heap bloat)
- Async Redis persistence (fire-and-forget, never blocks)
- No heavy dependencies (no Prometheus, no OpenTelemetry)
- Zero impact on critical path — all writes are non-blocking
- Metrics survive restart via Redis

Redis keys:
  metrics:global       HASH  — global counters
  metrics:tasks        LIST  — recent task records (capped at 500)
  metrics:skills       HASH  — per-skill counters
  metrics:daily:{date} HASH  — daily rollup (expires 7d)
"""

import asyncio
import json
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog

logger = structlog.get_logger()

# In-memory ring buffer size — won't grow beyond this
TASK_BUFFER_SIZE = 200
# Redis list cap for persisted task records
REDIS_TASK_CAP = 500


@dataclass
class TaskMetric:
    """Metrics for a single task execution."""
    task_id: str
    task_type: str          # "message" | "skill" | "scheduled"
    skill_name: str = ""
    model_used: str = ""
    provider: str = ""
    duration_ms: int = 0
    model_latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    memory_delta_kb: int = 0
    success: bool = True
    error: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class GlobalCounters:
    """In-memory global counters reset on restart."""
    tasks_total: int = 0
    tasks_success: int = 0
    tasks_failed: int = 0
    model_calls: int = 0
    skill_calls: int = 0
    skill_errors: int = 0
    autorepairs: int = 0
    tokens_total: int = 0
    cost_usd_total: float = 0.0
    scheduler_runs: int = 0
    scheduler_errors: int = 0


class MetricsCollector:
    """Thread-safe, async-first metrics collector.

    Never blocks the caller. Redis writes are background tasks.
    """

    def __init__(self, redis_url: str | None = None):
        self._redis_url = redis_url
        self._counters = GlobalCounters()
        self._task_buffer: deque[TaskMetric] = deque(maxlen=TASK_BUFFER_SIZE)
        self._skill_counts: dict[str, int] = {}
        self._skill_errors: dict[str, int] = {}
        self._start_time = time.monotonic()
        self._last_persist = 0.0
        # Per-skill latency tracking (last 50 per skill)
        self._skill_latencies: dict[str, deque] = {}

    # --- Recording API ---

    def record_task(self, metric: TaskMetric) -> None:
        """Record a completed task. Non-blocking."""
        self._task_buffer.append(metric)
        self._counters.tasks_total += 1
        if metric.success:
            self._counters.tasks_success += 1
        else:
            self._counters.tasks_failed += 1
        if metric.total_tokens:
            self._counters.tokens_total += metric.total_tokens
        if metric.cost_usd:
            self._counters.cost_usd_total += metric.cost_usd
        if metric.model_used:
            self._counters.model_calls += 1

    def record_skill(self, skill_name: str, duration_ms: int, success: bool) -> None:
        """Record a skill execution."""
        self._counters.skill_calls += 1
        self._skill_counts[skill_name] = self._skill_counts.get(skill_name, 0) + 1
        if not success:
            self._counters.skill_errors += 1
            self._skill_errors[skill_name] = self._skill_errors.get(skill_name, 0) + 1
        lat = self._skill_latencies.setdefault(skill_name, deque(maxlen=50))
        lat.append(duration_ms)

    def record_model_call(self, latency_ms: int, tokens: int, cost_usd: float) -> None:
        """Record a model API call."""
        self._counters.model_calls += 1
        self._counters.tokens_total += tokens
        self._counters.cost_usd_total += cost_usd

    def record_autorepair(self) -> None:
        self._counters.autorepairs += 1

    def record_scheduler_run(self, job_name: str, success: bool) -> None:
        self._counters.scheduler_runs += 1
        if not success:
            self._counters.scheduler_errors += 1

    # --- Query API ---

    def get_summary(self) -> dict:
        """Get current metrics summary. Always fast (in-memory)."""
        uptime_s = time.monotonic() - self._start_time
        total = self._counters.tasks_total or 1  # avoid div/0
        success_rate = round(self._counters.tasks_success / total * 100, 1)

        # Average latency from recent tasks
        recent = list(self._task_buffer)
        avg_duration = 0
        avg_model_latency = 0
        if recent:
            avg_duration = int(sum(t.duration_ms for t in recent) / len(recent))
            model_tasks = [t for t in recent if t.model_latency_ms]
            if model_tasks:
                avg_model_latency = int(
                    sum(t.model_latency_ms for t in model_tasks) / len(model_tasks)
                )

        return {
            "uptime_seconds": round(uptime_s),
            "tasks": {
                "total": self._counters.tasks_total,
                "success": self._counters.tasks_success,
                "failed": self._counters.tasks_failed,
                "success_rate_pct": success_rate,
            },
            "model": {
                "calls": self._counters.model_calls,
                "tokens_total": self._counters.tokens_total,
                "cost_usd_total": round(self._counters.cost_usd_total, 6),
                "avg_latency_ms": avg_model_latency,
            },
            "skills": {
                "calls": self._counters.skill_calls,
                "errors": self._counters.skill_errors,
                "top": [
                    {
                        "name": name,
                        "calls": count,
                        "errors": self._skill_errors.get(name, 0),
                    }
                    for name, count in sorted(
                        self._skill_counts.items(), key=lambda x: x[1], reverse=True
                    )[:8]
                ],
            },
            "scheduler": {
                "runs": self._counters.scheduler_runs,
                "errors": self._counters.scheduler_errors,
            },
            "autorepairs": self._counters.autorepairs,
            "performance": {
                "avg_task_duration_ms": avg_duration,
                "buffer_size": len(self._task_buffer),
            },
        }

    def get_skill_latency(self, skill_name: str) -> dict:
        """Get latency stats for a specific skill."""
        lats = list(self._skill_latencies.get(skill_name, []))
        if not lats:
            return {"skill": skill_name, "samples": 0}
        return {
            "skill": skill_name,
            "samples": len(lats),
            "avg_ms": round(sum(lats) / len(lats)),
            "min_ms": min(lats),
            "max_ms": max(lats),
            "p95_ms": sorted(lats)[int(len(lats) * 0.95)] if len(lats) >= 20 else max(lats),
        }

    def get_recent_tasks(self, limit: int = 20) -> list[dict]:
        """Return recent task records from ring buffer."""
        tasks = list(self._task_buffer)
        return [t.to_dict() for t in reversed(tasks[:limit])]

    # --- Persistence (non-blocking) ---

    def persist_async(self, task_metric: TaskMetric | None = None) -> None:
        """Fire-and-forget Redis persistence. Never blocks caller."""
        if not self._redis_url:
            return
        # Throttle: persist at most once every 30 seconds
        now = time.monotonic()
        if now - self._last_persist < 30 and task_metric is None:
            return
        self._last_persist = now
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._persist(task_metric))
        except RuntimeError:
            pass  # No event loop — skip

    async def _persist(self, task_metric: TaskMetric | None = None) -> None:
        """Write metrics to Redis in the background."""
        try:
            import redis.asyncio as aioredis
            r = aioredis.from_url(self._redis_url, decode_responses=True)

            pipe = r.pipeline(transaction=False)

            # Global counters
            counters = {
                "tasks_total": self._counters.tasks_total,
                "tasks_success": self._counters.tasks_success,
                "tasks_failed": self._counters.tasks_failed,
                "model_calls": self._counters.model_calls,
                "skill_calls": self._counters.skill_calls,
                "tokens_total": self._counters.tokens_total,
                "cost_usd_total": round(self._counters.cost_usd_total, 6),
                "autorepairs": self._counters.autorepairs,
            }
            pipe.hset("metrics:global", mapping={k: str(v) for k, v in counters.items()})

            # Per-skill counts
            if self._skill_counts:
                pipe.hset("metrics:skills", mapping={k: str(v) for k, v in self._skill_counts.items()})

            # Daily rollup
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            daily_key = f"metrics:daily:{today}"
            pipe.hincrby(daily_key, "tasks_total", 1)
            if task_metric:
                pipe.hincrby(daily_key, "tokens_total", task_metric.total_tokens)
                pipe.hincrbyfloat(daily_key, "cost_usd", task_metric.cost_usd)
            pipe.expire(daily_key, 86400 * 7)

            # Task record
            if task_metric:
                record = json.dumps(task_metric.to_dict())
                pipe.lpush("metrics:tasks", record)
                pipe.ltrim("metrics:tasks", 0, REDIS_TASK_CAP - 1)

            await pipe.execute()
            await r.aclose()
        except Exception:
            pass  # Metrics persistence is best-effort, never crash for this


# Module-level singleton — initialized with redis_url in main.py
metrics = MetricsCollector()
