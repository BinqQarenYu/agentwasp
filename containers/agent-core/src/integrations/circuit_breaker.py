"""Per-integration circuit breaker.

States:
    CLOSED   → Normal; all calls pass through
    OPEN     → Too many failures; reject immediately
    HALF_OPEN → Recovery probe; allow one call through to test

Transitions:
    CLOSED → OPEN       when failure_count >= threshold
    OPEN   → HALF_OPEN  after recovery_timeout seconds
    HALF_OPEN → CLOSED  on success
    HALF_OPEN → OPEN    on failure
"""

from __future__ import annotations

import json
import time
from enum import Enum
from typing import Any, Callable

import structlog

from .base import CircuitBreakerOpenError

try:
    import redis.asyncio as aioredis
    _REDIS_AVAILABLE = True
except ImportError:
    _REDIS_AVAILABLE = False

logger = structlog.get_logger()


class CircuitState(str, Enum):
    CLOSED    = "closed"
    OPEN      = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Asyncio-safe circuit breaker per integration with Redis-backed persistence.

    State survives agent-core restarts — a flapping integration that opened its
    breaker will stay open across deployments until it actually recovers.
    """

    _REDIS_PREFIX = "cb:state:"

    def __init__(
        self,
        integration_id: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        redis_url: str = "",
    ) -> None:
        self.integration_id    = integration_id
        self.failure_threshold = failure_threshold
        self.recovery_timeout  = recovery_timeout
        self._redis_url        = redis_url

        self._state: CircuitState     = CircuitState.CLOSED
        self._failure_count: int      = 0
        self._last_failure_time: float | None = None
        self._last_success_time: float | None = None
        self._restored: bool          = False   # True once Redis restore attempted

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def state(self) -> CircuitState:
        """Computed state — auto-transitions OPEN → HALF_OPEN after timeout."""
        if (
            self._state == CircuitState.OPEN
            and self._last_failure_time is not None
            and (time.monotonic() - self._last_failure_time) >= self.recovery_timeout
        ):
            self._state = CircuitState.HALF_OPEN
            logger.info(
                "circuit_breaker.half_open",
                integration=self.integration_id,
            )
        return self._state

    async def call(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        """Execute *fn* through the circuit breaker.

        Raises CircuitBreakerOpenError if state is OPEN.
        Any exception from *fn* is re-raised after recording the failure.
        """
        await self.restore()          # no-op after first call
        current = self.state

        if current == CircuitState.OPEN:
            raise CircuitBreakerOpenError(self.integration_id)

        try:
            result = await fn(*args, **kwargs)
            await self._on_success()
            return result
        except CircuitBreakerOpenError:
            raise
        except Exception:
            await self._on_failure()
            raise

    def reset(self) -> None:
        """Manually reset to CLOSED (admin action)."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = None
        self._restored = True  # prevent restore() from overwriting manual reset
        logger.info("circuit_breaker.manual_reset", integration=self.integration_id)
        # Best-effort async persist — fire-and-forget via asyncio if loop is running
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._persist())
        except RuntimeError:
            pass  # No running loop (sync context) — skip persist

    def to_dict(self) -> dict[str, Any]:
        return {
            "state":         self.state.value,
            "failure_count": self._failure_count,
            "threshold":     self.failure_threshold,
            "recovery_s":    self.recovery_timeout,
            "last_failure":  self._last_failure_time,
            "last_success":  self._last_success_time,
        }

    # ------------------------------------------------------------------
    # Redis persistence
    # ------------------------------------------------------------------

    async def restore(self) -> None:
        """Load persisted state from Redis on first call (one-shot).

        Allows breaker state to survive agent-core restarts. If Redis is
        unavailable or the key is absent, the breaker starts CLOSED as usual.
        """
        if self._restored:
            return
        self._restored = True

        if not self._redis_url or not _REDIS_AVAILABLE:
            return
        try:
            r = aioredis.from_url(self._redis_url, decode_responses=True)
            raw = await r.get(f"{self._REDIS_PREFIX}{self.integration_id}")
            await r.aclose()
            if not raw:
                return
            data = json.loads(raw)
            self._state             = CircuitState(data.get("state", CircuitState.CLOSED))
            self._failure_count     = int(data.get("failure_count", 0))
            self._last_failure_time = data.get("last_failure_time")
            self._last_success_time = data.get("last_success_time")
            logger.info(
                "circuit_breaker.restored",
                integration=self.integration_id,
                state=self._state.value,
                failures=self._failure_count,
            )
        except Exception:
            pass  # Restore failure must never block the call path

    async def _persist(self) -> None:
        """Write current breaker state to Redis (best-effort, never raises)."""
        if not self._redis_url or not _REDIS_AVAILABLE:
            return
        try:
            payload = json.dumps({
                "state":             self._state.value,
                "failure_count":     self._failure_count,
                "last_failure_time": self._last_failure_time,
                "last_success_time": self._last_success_time,
            })
            r = aioredis.from_url(self._redis_url, decode_responses=True)
            # TTL = recovery_timeout * 10 (at least 1 day). Expired = clean slate.
            ttl = max(86400, int(self.recovery_timeout * 10))
            await r.setex(f"{self._REDIS_PREFIX}{self.integration_id}", ttl, payload)
            await r.aclose()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internal transitions
    # ------------------------------------------------------------------

    async def _on_success(self) -> None:
        if self._state != CircuitState.CLOSED:
            logger.info(
                "circuit_breaker.recovered",
                integration=self.integration_id,
                was=self._state.value,
            )
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_success_time = time.monotonic()
        await self._persist()

    async def _on_failure(self) -> None:
        self._failure_count += 1
        self._last_failure_time = time.monotonic()

        if self._failure_count >= self.failure_threshold:
            if self._state != CircuitState.OPEN:
                logger.warning(
                    "circuit_breaker.opened",
                    integration=self.integration_id,
                    failures=self._failure_count,
                    threshold=self.failure_threshold,
                )
            self._state = CircuitState.OPEN
        await self._persist()
