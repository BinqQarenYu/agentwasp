"""Performance tracking utilities for WASP agent.

Provides:
- ExecutionTimer: async context manager for timing any block
- MemoryDelta: before/after RSS measurement (optional)
- ConcurrencyGuard: max concurrent task enforcement
- Degradation detector: detects sustained latency degradation
"""

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncGenerator

import structlog

logger = structlog.get_logger()


@dataclass
class TimingResult:
    """Result from an ExecutionTimer context."""
    label: str
    duration_ms: int
    start_ts: str
    success: bool = True
    error: str = ""


@asynccontextmanager
async def execution_timer(label: str) -> AsyncGenerator[TimingResult, None]:
    """Async context manager that measures execution time.

    Usage:
        async with execution_timer("my_task") as t:
            await do_work()
        print(t.duration_ms)
    """
    result = TimingResult(
        label=label,
        duration_ms=0,
        start_ts=datetime.now(timezone.utc).isoformat(),
    )
    start = time.monotonic()
    try:
        yield result
        result.success = True
    except Exception as e:
        result.success = False
        result.error = str(e)[:200]
        raise
    finally:
        result.duration_ms = int((time.monotonic() - start) * 1000)


def get_rss_kb() -> int:
    """Get current process RSS in KB. Returns 0 if psutil unavailable."""
    try:
        import os
        import psutil
        return int(psutil.Process(os.getpid()).memory_info().rss / 1024)
    except Exception:
        return 0


class MemoryDelta:
    """Measures memory delta for a code block.

    Usage:
        with MemoryDelta() as md:
            do_work()
        print(md.delta_kb)  # positive = grew, negative = shrank
    """

    def __init__(self):
        self.before_kb = 0
        self.after_kb = 0
        self.delta_kb = 0

    def __enter__(self):
        self.before_kb = get_rss_kb()
        return self

    def __exit__(self, *args):
        self.after_kb = get_rss_kb()
        self.delta_kb = self.after_kb - self.before_kb


class ConcurrencyGuard:
    """Limits the number of concurrent task executions.

    Non-blocking: returns False immediately if limit is reached.
    This prevents resource exhaustion under load.
    """

    def __init__(self, max_concurrent: int = 10):
        self._max = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active = 0

    @property
    def active(self) -> int:
        return self._active

    @property
    def available(self) -> bool:
        return self._semaphore._value > 0  # type: ignore[attr-defined]

    @asynccontextmanager
    async def acquire(self, timeout: float = 30.0) -> AsyncGenerator[bool, None]:
        """Acquire a slot. Raises asyncio.TimeoutError if timeout exceeded."""
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=timeout)
            self._active += 1
            try:
                yield True
            finally:
                self._active -= 1
                self._semaphore.release()
        except asyncio.TimeoutError:
            logger.warning(
                "concurrency_guard.timeout",
                active=self._active,
                max=self._max,
            )
            yield False

    def status(self) -> dict:
        return {
            "max_concurrent": self._max,
            "active": self._active,
            "available_slots": self._max - self._active,
        }


class DegradationDetector:
    """Detects sustained latency or error-rate degradation.

    Tracks a rolling window of recent measurements.
    Emits a warning when degradation is sustained for > threshold_count consecutive checks.
    """

    def __init__(
        self,
        window_size: int = 20,
        latency_threshold_ms: int = 15000,
        error_rate_threshold: float = 0.20,
        consecutive_threshold: int = 3,
    ):
        self._window_size = window_size
        self._latency_threshold = latency_threshold_ms
        self._error_rate_threshold = error_rate_threshold
        self._consecutive_threshold = consecutive_threshold
        self._latencies: list[int] = []
        self._errors: list[bool] = []
        self._consecutive_slow = 0
        self._consecutive_errors = 0
        self.degraded = False
        self.degradation_reason = ""

    def record(self, latency_ms: int, is_error: bool) -> bool:
        """Record a measurement. Returns True if degradation detected."""
        # Rolling window
        self._latencies.append(latency_ms)
        if len(self._latencies) > self._window_size:
            self._latencies.pop(0)

        self._errors.append(is_error)
        if len(self._errors) > self._window_size:
            self._errors.pop(0)

        # Check consecutive slow responses
        if latency_ms > self._latency_threshold:
            self._consecutive_slow += 1
        else:
            self._consecutive_slow = 0

        # Check error rate
        if len(self._errors) >= 5:
            error_rate = sum(self._errors) / len(self._errors)
            if error_rate > self._error_rate_threshold:
                self._consecutive_errors += 1
            else:
                self._consecutive_errors = 0
        else:
            self._consecutive_errors = 0

        # Determine degradation state
        prev_degraded = self.degraded

        if self._consecutive_slow >= self._consecutive_threshold:
            self.degraded = True
            self.degradation_reason = f"high_latency ({latency_ms}ms for {self._consecutive_slow} calls)"
        elif self._consecutive_errors >= self._consecutive_threshold:
            self.degraded = True
            self.degradation_reason = f"high_error_rate ({sum(self._errors)}/{len(self._errors)})"
        else:
            self.degraded = False
            self.degradation_reason = ""

        if self.degraded and not prev_degraded:
            logger.warning(
                "degradation_detector.degraded",
                reason=self.degradation_reason,
            )
        elif not self.degraded and prev_degraded:
            logger.info("degradation_detector.recovered")

        return self.degraded

    def get_stats(self) -> dict:
        if not self._latencies:
            return {"degraded": False, "samples": 0}
        avg_lat = sum(self._latencies) / len(self._latencies)
        error_rate = sum(self._errors) / max(len(self._errors), 1)
        return {
            "degraded": self.degraded,
            "reason": self.degradation_reason,
            "samples": len(self._latencies),
            "avg_latency_ms": round(avg_lat),
            "p95_latency_ms": sorted(self._latencies)[int(len(self._latencies) * 0.95)]
                if len(self._latencies) >= 5 else max(self._latencies),
            "error_rate_pct": round(error_rate * 100, 1),
        }


# Module-level singletons
concurrency_guard = ConcurrencyGuard(max_concurrent=10)
degradation_detector = DegradationDetector()
