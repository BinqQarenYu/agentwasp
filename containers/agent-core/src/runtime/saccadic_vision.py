"""Saccadic Vision — real-time system-state observer (WASP v2.5).

Runs as an independent daemon thread.  Collects lightweight system metrics
every cycle and publishes a structured snapshot to the Redis saccadic stream.

Design constraints
------------------
- Completely decoupled from core execution — no imports from handlers/LLM
- Never consumes LLM tokens
- Never blocks the main asyncio loop (runs in its own thread with sync Redis)
- Self-healing: all errors are caught, logged, and the loop continues
- Uses psutil (already a project dependency) for non-blocking metrics

Redis stream
------------
Events are published to ``events:saccadic`` (maxlen=500, approximate).

Event payload
-------------
{
    "type":         "system_snapshot",
    "timestamp":    "<ISO-8601>",
    "cpu":          <float — percent>,
    "memory":       <float — percent>,
    "active_goals": <int>,
    "prev_hash":    "<hex>",
    "curr_hash":    "<hex>",
    "changed":      "true" | "false"
}

Heartbeat
---------
A snapshot is always emitted at least every HEARTBEAT_CYCLES cycles (default 30
cycles × 2 s interval = 60 s) so the stream is never empty.  Additional events
fire immediately on significant state changes (CPU/memory bucket shift or
goal-count change).

Usage
-----
    sv = SaccadicVision(redis_url="redis://localhost:6379")
    sv.start()
    # sv.stop()
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

SACCADIC_STREAM = "events:saccadic"
SACCADIC_MAXLEN = 500

# Emit a heartbeat snapshot at least every N cycles (cycle ≈ interval seconds)
HEARTBEAT_CYCLES = 30


class SaccadicVision:
    """Lightweight system-state observer running on a daemon thread."""

    def __init__(self, redis_url: str = "", interval: float = 2.0) -> None:
        self.redis_url = redis_url
        self.interval = interval
        self.prev_hash: str | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_emit_ts: float = 0.0
        self.debounce_seconds: float = 5.0
        self._cycle: int = 0

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the detection loop on a daemon thread."""
        self._thread = threading.Thread(
            target=self._run_loop,
            name="saccadic-vision",
            daemon=True,
        )
        self._thread.start()
        logger.debug("saccadic_vision.started interval=%.1fs", self.interval)

    def stop(self) -> None:
        """Signal the loop to stop; returns immediately (non-blocking)."""
        self._stop_event.set()

    # ── Core primitives ───────────────────────────────────────────────────────

    @staticmethod
    def compute_hash(data: bytes) -> str:
        return hashlib.sha1(data).hexdigest()  # noqa: S324

    @staticmethod
    def compute_diff(prev_hash: str, current_hash: str) -> float:
        return 0.0 if prev_hash == current_hash else 100.0

    # ── Metrics collection ────────────────────────────────────────────────────

    def _collect_metrics(self) -> dict:
        """Collect lightweight system metrics synchronously (non-blocking).

        psutil.cpu_percent(interval=None) returns the cached value from the
        last call — it does NOT sleep or block.
        """
        cpu = 0.0
        mem = 0.0
        try:
            import psutil
            cpu = round(psutil.cpu_percent(interval=None), 1)
            mem = round(psutil.virtual_memory().percent, 1)
        except Exception:
            pass

        goal_count = 0
        if self.redis_url:
            try:
                import redis as _redis_sync
                r = _redis_sync.from_url(
                    self.redis_url, decode_responses=True, socket_connect_timeout=1
                )
                try:
                    keys = r.keys("goal:*")
                    goal_count = len(keys) if keys else 0
                finally:
                    r.close()
            except Exception:
                pass

        return {"cpu": cpu, "memory": mem, "active_goals": goal_count}

    def capture_state(self) -> bytes:
        """Return bytes representing current bucketed state for change detection.

        Values are bucketed to 5% increments so the hash only changes on
        meaningful transitions — not on every minor CPU fluctuation.
        Timestamp is excluded from the hash.
        """
        m = self._collect_metrics()
        bucketed = {
            "cpu_bucket":   int(m["cpu"] / 5) * 5,
            "mem_bucket":   int(m["memory"] / 5) * 5,
            "goal_count":   m["active_goals"],
        }
        return json.dumps(bucketed, sort_keys=True).encode()

    # ── Event emission ────────────────────────────────────────────────────────

    def emit_event(self, prev_hash: str, curr_hash: str, changed: bool = False) -> None:
        """Publish a system snapshot to the Redis saccadic stream.

        Fails silently — a missed event is always preferable to a crash.
        """
        metrics = self._collect_metrics()
        payload = {
            "type":         "system_snapshot",
            "timestamp":    datetime.now(timezone.utc).isoformat(),
            "cpu":          str(metrics["cpu"]),
            "memory":       str(metrics["memory"]),
            "active_goals": str(metrics["active_goals"]),
            "prev_hash":    prev_hash,
            "curr_hash":    curr_hash,
            "changed":      "true" if changed else "false",
        }

        if not self.redis_url:
            logger.debug("saccadic_snapshot payload=%s", json.dumps(payload))
            return

        try:
            import redis as _redis_sync
            r = _redis_sync.from_url(
                self.redis_url, decode_responses=True, socket_connect_timeout=1
            )
            try:
                r.xadd(SACCADIC_STREAM, payload, maxlen=SACCADIC_MAXLEN, approximate=True)
                # CAUSAL ACTUATION: set/clear agent:cpi_high flag directly from saccadic
                # readings so high-CPU backpressure responds in <2s instead of waiting for
                # the 5-min CPI monitor job.
                cpu = metrics["cpu"]
                if cpu >= 85.0:
                    r.set("agent:saccadic_cpu_high", "1", ex=120)  # 2-minute TTL
                    logger.info("saccadic_vision.cpu_high_flag_set", cpu=cpu)
                else:
                    r.delete("agent:saccadic_cpu_high")
                logger.debug(
                    "saccadic_snapshot_emitted cpu=%.1f mem=%.1f goals=%d changed=%s",
                    metrics["cpu"], metrics["memory"], metrics["active_goals"], changed,
                )
            finally:
                r.close()
        except Exception as exc:
            logger.debug("saccadic_vision.emit_error error=%r", str(exc)[:80])

    # ── Detection loop ────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        """Main observation loop — runs on daemon thread until stop() is called."""
        logger.debug("saccadic_vision.loop_start")
        while not self._stop_event.is_set():
            try:
                data = self.capture_state()
                curr_hash = self.compute_hash(data)
                self._cycle += 1

                # Determine whether to emit
                should_emit = False
                changed = False

                if self._cycle >= HEARTBEAT_CYCLES:
                    # Forced heartbeat — always emit
                    should_emit = True
                    self._cycle = 0
                elif self.prev_hash is not None:
                    if self.compute_diff(self.prev_hash, curr_hash) > 0:
                        now = time.time()
                        if now - self.last_emit_ts > self.debounce_seconds:
                            should_emit = True
                            changed = True

                if should_emit:
                    prev_h = self.prev_hash or curr_hash
                    self.emit_event(prev_h, curr_hash, changed=changed)
                    self.last_emit_ts = time.time()

                self.prev_hash = curr_hash

            except Exception as exc:
                logger.debug("saccadic_vision.loop_error error=%r", str(exc)[:120])

            self._stop_event.wait(timeout=self.interval)

        logger.debug("saccadic_vision.loop_stopped")
