"""PolicyEngine — gates every integration action.

Rules (evaluated in order):
    1. CRITICAL risk → always blocked
    2. Integration disabled → blocked
    3. Specific action blocked → blocked
    4. HIGH risk + autonomy=assist → blocked (requires confirmation)
    5. Otherwise → allowed

State (enabled integrations + blocked actions) is persisted in Redis
so toggles survive restarts.
"""

from __future__ import annotations

from dataclasses import dataclass

import redis.asyncio as aioredis
import structlog

from .base import RiskLevel

logger = structlog.get_logger()

_NS = "integrations:policy:"


@dataclass
class GateResult:
    allowed: bool
    reason: str = ""


class PolicyEngine:
    """Gate all integration action executions.

    Autonomy modes (mirrors GoalOrchestrator.AutonomyMode):
        assist  — operator confirms HIGH; CRITICAL always blocked
        semi    — auto-approve up to HIGH; CRITICAL always blocked  (default)
        full    — auto-approve up to HIGH; CRITICAL always blocked
    """

    def __init__(self, redis_url: str, autonomy_mode: str = "semi") -> None:
        self._redis_url = redis_url
        self._mode = autonomy_mode.lower()
        self._enabled: set[str] = set()
        self._blocked_actions: set[str] = set()   # "integration_id:action_id"

    async def initialize(self) -> None:
        """Load persisted state from Redis."""
        r = aioredis.from_url(self._redis_url, decode_responses=True)
        try:
            enabled = await r.smembers(f"{_NS}enabled")
            self._enabled = set(enabled)
            blocked = await r.smembers(f"{_NS}blocked_actions")
            self._blocked_actions = set(blocked)
            logger.info(
                "policy_engine.initialized",
                enabled=len(self._enabled),
                blocked_actions=len(self._blocked_actions),
            )
        except Exception:
            logger.warning("policy_engine.redis_init_failed_using_empty_state")
        finally:
            await r.aclose()

    # ------------------------------------------------------------------
    # Gate (called on every execute)
    # ------------------------------------------------------------------

    async def gate(
        self,
        integration_id: str,
        action_id: str,
        risk_level: RiskLevel,
    ) -> GateResult:
        """Return GateResult(allowed=True) if action may proceed."""
        # Rule 1: CRITICAL always blocked
        if risk_level == RiskLevel.CRITICAL:
            return GateResult(
                False,
                "CRITICAL risk actions are always blocked and require manual execution",
            )

        # Rule 2: Integration must be enabled
        if integration_id not in self._enabled:
            return GateResult(False, f"Integration '{integration_id}' is not enabled")

        # Rule 3: Specific action may be blocked
        if f"{integration_id}:{action_id}" in self._blocked_actions:
            return GateResult(False, f"Action '{action_id}' is blocked by policy")

        # Rule 4: HIGH risk requires non-assist mode
        if risk_level == RiskLevel.HIGH and self._mode == "assist":
            return GateResult(
                False,
                "HIGH risk actions require confirmation in ASSIST autonomy mode",
            )

        return GateResult(True)

    # ------------------------------------------------------------------
    # Enable / disable integrations
    # ------------------------------------------------------------------

    async def enable(self, integration_id: str) -> None:
        self._enabled.add(integration_id)
        await self._redis_op("sadd", f"{_NS}enabled", integration_id)
        logger.info("policy_engine.integration_enabled", id=integration_id)

    async def disable(self, integration_id: str) -> None:
        self._enabled.discard(integration_id)
        await self._redis_op("srem", f"{_NS}enabled", integration_id)
        logger.info("policy_engine.integration_disabled", id=integration_id)

    def is_enabled(self, integration_id: str) -> bool:
        return integration_id in self._enabled

    # ------------------------------------------------------------------
    # Block / unblock specific actions
    # ------------------------------------------------------------------

    async def block_action(self, integration_id: str, action_id: str) -> None:
        key = f"{integration_id}:{action_id}"
        self._blocked_actions.add(key)
        await self._redis_op("sadd", f"{_NS}blocked_actions", key)

    async def unblock_action(self, integration_id: str, action_id: str) -> None:
        key = f"{integration_id}:{action_id}"
        self._blocked_actions.discard(key)
        await self._redis_op("srem", f"{_NS}blocked_actions", key)

    def set_autonomy_mode(self, mode: str) -> None:
        self._mode = mode.lower()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _redis_op(self, op: str, *args) -> None:
        r = aioredis.from_url(self._redis_url, decode_responses=True)
        try:
            await getattr(r, op)(*args)
        except Exception:
            logger.warning("policy_engine.redis_persist_failed", op=op)
        finally:
            await r.aclose()
