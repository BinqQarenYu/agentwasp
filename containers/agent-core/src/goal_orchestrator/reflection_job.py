"""GoalMetaReflectionJob — analyzes active goals for anomalies.

Scheduled as a periodic job (default every 5 minutes).  For each active
goal it evaluates:

  - replan_frequency   : replans / steps (high ratio = bad planning)
  - failure_cluster    : consecutive_failures >= 2
  - budget_velocity    : planning token consumption > 60% of budget
  - policy_block_rate  : blocks / steps > 30%
  - stagnation         : no DONE tasks after STAGNATION_STEPS steps

On anomaly:
  1. Call LLM for structured suggestion (max MAX_REFLECTION_TOKENS)
  2. Apply intervention: pause (if severe) or monitor
  3. Emit META_REFLECTION_ANALYSIS event to audit_log
  4. Notify user via Telegram if goal is paused

Design constraints:
  - No recursive LLM calls
  - Strict token budget per reflection call
  - Structured JSON output only
  - Never escalates privileges (read-only on non-severe anomalies)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import structlog

from ..models.manager import ModelManager
from ..models.types import Message, ModelRequest
from .store import list_active_goals, save_goal
from .types import Goal, GoalEvent, GoalState, TaskStatus

logger = structlog.get_logger()

MAX_REFLECTION_TOKENS = 400       # Tight cap: this fires frequently
STAGNATION_STEPS = 10             # No DONE tasks after N steps → stagnation
REPLAN_RATE_THRESHOLD = 0.5       # replans/steps above this is concerning
POLICY_BLOCK_RATE_THRESHOLD = 0.3  # policy_blocks/steps above this is concerning
BUDGET_WARNING_PCT = 0.85         # Warn at 85% of planning token budget (was 0.6 — too aggressive)

# Anomalies that alone should never trigger a pause — only monitor
_NON_PAUSE_ANOMALIES = frozenset({"high_budget_consumption", "high_replan_rate"})


# ---------------------------------------------------------------------------
# Anomaly detection (pure functions — no I/O)
# ---------------------------------------------------------------------------


def detect_anomalies(goal: Goal) -> list[str]:
    """Return list of anomaly identifiers for a goal. Empty = healthy."""
    anomalies: list[str] = []

    if goal.stability.locked:
        anomalies.append("locked_stability")

    if goal.stability.consecutive_failures >= 3:
        anomalies.append("failure_cluster")

    if goal.step_count >= STAGNATION_STEPS:
        done_count = sum(
            1 for n in goal.task_graph.nodes if n.status == TaskStatus.DONE
        )
        if done_count == 0:
            anomalies.append("stagnation")

    if goal.step_count > 3:
        replan_rate = goal.replan_count / max(goal.step_count, 1)
        if replan_rate >= REPLAN_RATE_THRESHOLD and goal.replan_count >= 2:
            anomalies.append("high_replan_rate")

        if goal.step_count > 5:
            block_rate = goal.telemetry.policy_blocks / max(goal.step_count, 1)
            if block_rate >= POLICY_BLOCK_RATE_THRESHOLD:
                anomalies.append("frequent_policy_blocks")

    if goal.budget.max_tokens_planning > 0:
        pct = goal.budget.tokens_used_planning / goal.budget.max_tokens_planning
        if pct >= BUDGET_WARNING_PCT:
            anomalies.append("high_budget_consumption")

    return anomalies


# ---------------------------------------------------------------------------
# Severity classifier
# ---------------------------------------------------------------------------


def is_severe(anomalies: list[str]) -> bool:
    """Return True if anomalies warrant an automatic pause intervention."""
    severe = {"stagnation", "locked_stability"}
    return bool(severe & set(anomalies))


# ---------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------


class GoalMetaReflectionJob:
    """Scheduler-compatible callable that reflects on active goals.

    Usage in main.py:
        scheduler.register(
            "goal_meta_reflection",
            settings.goal_meta_reflection_interval,
            GoalMetaReflectionJob(
                model_manager=model_manager,
                redis_url=settings.redis_url,
                bus=bus,
                notify_chat_id=settings.scheduler_notify_chat_id,
            ),
        )
    """

    def __init__(
        self,
        model_manager: ModelManager,
        redis_url: str,
        bus=None,
        notify_chat_id: str = "",
    ):
        self.model_manager = model_manager
        self.redis_url = redis_url
        self.bus = bus
        self.notify_chat_id = notify_chat_id

    async def __call__(self) -> str:
        import redis.asyncio as aioredis

        r = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            goals = await list_active_goals(r)
        finally:
            await r.aclose()

        if not goals:
            return "0 active goals"

        analyzed = 0
        intervened = 0

        for goal in goals:
            anomalies = detect_anomalies(goal)
            if not anomalies:
                continue

            analyzed += 1
            suggestion_text = await self._get_suggestion(goal, anomalies)
            action = await self._apply(goal, anomalies, suggestion_text)
            intervened += 1 if action != "monitored" else 0

            if action != "monitored":
                goal.telemetry.stability_interventions += 1
                r = aioredis.from_url(self.redis_url, decode_responses=True)
                try:
                    await save_goal(r, goal)
                finally:
                    await r.aclose()

            await self._emit(
                goal,
                json.dumps({
                    "anomalies": anomalies,
                    "suggestion": suggestion_text[:200],
                    "action": action,
                }, ensure_ascii=False)[:400],
            )

        return (
            f"Reflected {analyzed} anomalous / {len(goals)} active goals; "
            f"{intervened} interventions"
        )

    async def _get_suggestion(self, goal: Goal, anomalies: list[str]) -> str:
        """Call LLM for a structured suggestion with a hard token cap."""
        prompt = (
            f"Autonomous AI goal executor meta-reflection.\n\n"
            f"Goal: {goal.objective[:150]}\n"
            f"Progress: {goal.progress:.0f}%  Steps: {goal.step_count}/{goal.budget.max_steps}\n"
            f"Replans: {goal.replan_count}  Failures: {goal.stability.consecutive_failures}\n"
            f"Anomalies: {', '.join(anomalies)}\n\n"
            "Output JSON only:\n"
            '{"recommendation":"simplify|pause|continue","reason":"<20 words>","simplified_objective":"<if simplify>"}'
        )
        try:
            response = await self.model_manager.generate(
                ModelRequest(
                    messages=[
                        Message(
                            role="system",
                            content="Output ONLY valid JSON. Be concise.",
                        ),
                        Message(role="user", content=prompt),
                    ],
                    temperature=0.2,
                    max_tokens=MAX_REFLECTION_TOKENS,
                )
            )
            return response.content.strip()
        except Exception as e:
            logger.warning(
                "meta_reflection.llm_error", goal_id=goal.id, error=str(e)
            )
            return '{"recommendation":"pause","reason":"reflection LLM error"}'

    async def _apply(
        self,
        goal: Goal,
        anomalies: list[str],
        suggestion_text: str,
    ) -> str:
        """Apply intervention. Returns action string."""
        # Parse suggestion safely
        recommendation = "continue"
        reason = ", ".join(anomalies)
        try:
            suggestion = json.loads(suggestion_text)
            recommendation = suggestion.get("recommendation", "continue")
            reason = suggestion.get("reason", reason)[:200]
        except Exception:
            pass

        # If all anomalies are non-critical, never pause — just monitor
        if set(anomalies) <= _NON_PAUSE_ANOMALIES:
            logger.info(
                "meta_reflection.monitored_non_critical",
                goal_id=goal.id,
                anomalies=anomalies,
            )
            return "monitored"

        # Severe anomalies always trigger pause, regardless of LLM suggestion
        if is_severe(anomalies) or recommendation == "pause":
            goal.state = GoalState.PAUSED
            goal.stability.last_intervention = datetime.now(timezone.utc).isoformat()
            goal.stability.intervention_reason = f"meta-reflection: {reason}"
            # Clean objective — strip scheduled task prefix before showing to user
            import re as _re
            _obj = _re.sub(r"\[TAREA PROGRAMADA:[^\]]*\]\s*\n?EJECUTA AHORA[^\n]*\n?", "", goal.objective).strip()
            await self._notify(
                goal,
                f"⚠️ Goal pausado por meta-reflection.\n"
                f"Razón: {reason}\n"
                f"_{_obj[:80]}_",
            )
            logger.warning(
                "meta_reflection.paused_goal",
                goal_id=goal.id,
                anomalies=anomalies,
                reason=reason,
            )
            return "paused"

        # Non-severe: just log
        logger.info(
            "meta_reflection.monitored",
            goal_id=goal.id,
            anomalies=anomalies,
            recommendation=recommendation,
        )
        return "monitored"

    async def _notify(self, goal: Goal, text: str) -> None:
        chat_id = goal.chat_id or self.notify_chat_id
        if not self.bus or not chat_id:
            return
        try:
            await self.bus.publish(
                "events:outgoing",
                {
                    "event_type": "telegram.response",
                    "chat_id": chat_id,
                    "text": text,
                    "correlation_id": str(uuid4()),
                    "metadata": "{}",
                },
            )
        except Exception:
            logger.exception("meta_reflection.notify_error", goal_id=goal.id)

    async def _emit(self, goal: Goal, detail: str) -> None:
        try:
            logger.info(
                "goal_event.meta_reflection.analysis",
                goal_id=goal.id,
                detail=detail[:120],
            )
            from ..db.models import AuditLog
            from ..db.session import async_session

            async with async_session() as session:
                audit = AuditLog(
                    id=str(uuid4()),
                    event_type="goal.meta_reflection",
                    source="goal_meta_reflection",
                    action="meta_reflection.analysis",
                    input_summary=goal.id,
                    output_summary=detail[:200],
                    user_id="",
                    chat_id=goal.chat_id,
                )
                session.add(audit)
                await session.commit()
        except Exception:
            logger.exception("meta_reflection.emit_error", goal_id=goal.id)
