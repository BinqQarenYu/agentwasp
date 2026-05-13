"""Per-integration observability metrics.

Tracks (in-memory rolling window, no Redis overhead):
    - Latency histogram → p50 / p95 / p99
    - Error rate (rolling last N calls)
    - Total call / success / failure counts
    - Retry count
    - Last error message + timestamp
    - Sparkline data (success rate over time buckets)

No heavy chart libraries required — sparkline data is a list[float]
consumed by the SVG sparkline renderer already in app.js.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any


class IntegrationMetrics:
    """Rolling-window metrics for a single integration."""

    def __init__(self, integration_id: str, window_size: int = 500) -> None:
        self.integration_id = integration_id
        # (monotonic_ts, latency_ms, success: bool)
        self._window: deque[tuple[float, float, bool]] = deque(maxlen=window_size)
        self._total_calls   = 0
        self._total_success = 0
        self._total_failure = 0
        self._total_retries = 0
        self._last_error: str | None = None
        self._last_error_ts: float | None = None
        self._start_ts = time.monotonic()

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    async def record_success(self, latency_ms: float) -> None:
        self._total_calls   += 1
        self._total_success += 1
        self._window.append((time.monotonic(), latency_ms, True))

    async def record_failure(self, error: str, latency_ms: float) -> None:
        self._total_calls   += 1
        self._total_failure += 1
        self._last_error    = error[:300]
        self._last_error_ts = time.monotonic()
        self._window.append((time.monotonic(), latency_ms, False))

    async def record_retry(self) -> None:
        self._total_retries += 1

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        items = list(self._window)
        error_rate = 0.0
        if items:
            error_rate = sum(1 for _, _, ok in items if not ok) / len(items)

        uptime = max(time.monotonic() - self._start_ts, 1.0)
        return {
            "integration_id":        self.integration_id,
            "total_calls":           self._total_calls,
            "total_success":         self._total_success,
            "total_failure":         self._total_failure,
            "total_retries":         self._total_retries,
            "error_rate":            round(error_rate, 4),
            "calls_per_second":      round(self._total_calls / uptime, 4),
            "latency_p50_ms":        self._percentile(50),
            "latency_p95_ms":        self._percentile(95),
            "latency_p99_ms":        self._percentile(99),
            "last_error":            self._last_error,
            "last_error_ts":         self._last_error_ts,
            "window_count":          len(self._window),
        }

    def sparkline_data(self, buckets: int = 20) -> list[float]:
        """Success-rate data per time bucket for sparkline rendering."""
        items = list(self._window)
        if not items:
            return [1.0] * buckets

        bucket_size = max(len(items) // buckets, 1)
        result: list[float] = []
        for i in range(buckets):
            start = i * bucket_size
            end   = min(start + bucket_size, len(items))
            if start >= len(items):
                result.append(1.0)
                continue
            bucket = items[start:end]
            rate   = sum(1 for _, _, ok in bucket if ok) / len(bucket)
            result.append(round(rate, 2))
        return result

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _percentile(self, p: float) -> float | None:
        latencies = sorted(x[1] for x in self._window)
        if not latencies:
            return None
        idx = int(len(latencies) * p / 100)
        return round(latencies[min(idx, len(latencies) - 1)], 2)
