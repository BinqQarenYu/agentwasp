"""Stability Layer for the Autonomous Goal Engine.

Prevents infinite replan loops and runaway failure cascades through
four hard rules:

  1. Consecutive failures → exponential backoff
  2. Replan storm (N replans in window) → pause goal
  3. Oscillation detection (repeating error signatures) → lock goal
  4. Repeated policy blocks on same capability → block capability

All functions are synchronous and operate on StabilityState.
The caller (executor / orchestrator) persists the updated Goal.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .types import StabilityState

# ---------------------------------------------------------------------------
# Thresholds (conservative defaults)
# ---------------------------------------------------------------------------

CONSECUTIVE_FAILURES_THRESHOLD = 4    # Trigger backoff after N failures
# Replan storm: complex multi-skill goals (BTC+ETH+SOL crypto report) often
# need 3-4 replans legitimately when a source blocks or a step fails. The
# previous 3-replans-in-5min cap fired storm exactly when the goal was
# making progress. Loosened to 5 / 10 minutes — still catches actual loops,
# stops penalizing real complex work.
REPLAN_STORM_COUNT = 5
REPLAN_STORM_WINDOW_MINUTES = 10
POLICY_BLOCK_LIMIT = 3                 # N blocks on same capability → lock capability
OSCILLATION_HISTORY_LEN = 3           # Match last N error signatures


# ---------------------------------------------------------------------------
# Task outcome updates
# ---------------------------------------------------------------------------


def on_task_success(stability: StabilityState) -> None:
    """Reset consecutive failure counter on task success."""
    stability.consecutive_failures = 0


def on_task_failure(stability: StabilityState, error: str = "") -> None:
    """Increment consecutive failure counter; apply backoff if threshold reached."""
    stability.consecutive_failures += 1


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------


def apply_backoff_if_needed(stability: StabilityState) -> bool:
    """Apply exponential backoff if consecutive failures >= threshold.

    Delay formula: min(2^(failures - threshold + 1), 300) seconds.
    Returns True if backoff was applied.
    """
    if stability.consecutive_failures < CONSECUTIVE_FAILURES_THRESHOLD:
        return False
    exponent = stability.consecutive_failures - CONSECUTIVE_FAILURES_THRESHOLD + 1
    delay_seconds = min(2 ** exponent, 300)
    until = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
    stability.backoff_until = until.isoformat()
    stability.in_backoff = True
    return True


def check_backoff(stability: StabilityState) -> tuple[bool, str]:
    """Return (is_in_backoff, reason) for the current moment.

    Automatically clears expired backoff.
    """
    if not stability.backoff_until:
        return False, ""
    try:
        until = datetime.fromisoformat(stability.backoff_until)
        now = datetime.now(timezone.utc)
        if now < until:
            remaining = int((until - now).total_seconds())
            return True, f"In backoff for {remaining}s more (consecutive failures: {stability.consecutive_failures})"
    except Exception:
        pass
    # Backoff expired — clear it
    stability.backoff_until = None
    stability.in_backoff = False
    return False, ""


# ---------------------------------------------------------------------------
# Replan storm detection
# ---------------------------------------------------------------------------


def record_replan(stability: StabilityState) -> bool:
    """Record a replanning event.  Returns True if a replan storm is detected.

    Storm = REPLAN_STORM_COUNT replans within REPLAN_STORM_WINDOW_MINUTES.
    """
    now = datetime.now(timezone.utc)
    stability.replan_timestamps.append(now.isoformat())
    # Keep only the most recent entries
    stability.replan_timestamps = stability.replan_timestamps[-20:]

    window_start = now - timedelta(minutes=REPLAN_STORM_WINDOW_MINUTES)
    recent = [
        ts for ts in stability.replan_timestamps
        if _parse_iso(ts) >= window_start
    ]
    return len(recent) >= REPLAN_STORM_COUNT


# ---------------------------------------------------------------------------
# Oscillation detection
# ---------------------------------------------------------------------------


def detect_oscillation(error_history: list[str]) -> bool:
    """Return True if the last N errors are identical (repeated failure pattern)."""
    if len(error_history) < OSCILLATION_HISTORY_LEN:
        return False
    tail = error_history[-OSCILLATION_HISTORY_LEN:]
    return len(set(tail)) == 1 and tail[0] != ""


def lock_goal(stability: StabilityState, reason: str) -> None:
    """Lock the goal due to oscillating plan."""
    stability.locked = True
    stability.last_intervention = datetime.now(timezone.utc).isoformat()
    stability.intervention_reason = reason


# ---------------------------------------------------------------------------
# Policy block tracking
# ---------------------------------------------------------------------------


def record_policy_block(stability: StabilityState, capability: str) -> bool:
    """Record a policy block for a capability.

    Returns True if the block limit for this capability has been reached.
    """
    if not capability:
        return False
    count = stability.policy_block_counts.get(capability, 0) + 1
    stability.policy_block_counts[capability] = count
    return count >= POLICY_BLOCK_LIMIT


# ---------------------------------------------------------------------------
# Intervention recording
# ---------------------------------------------------------------------------


def record_intervention(stability: StabilityState, reason: str) -> None:
    stability.last_intervention = datetime.now(timezone.utc).isoformat()
    stability.intervention_reason = reason[:300]


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _parse_iso(ts: str) -> datetime:
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)
