"""GoalStepExecutor — executes ONE task step for a given Goal.

Design constraints:
  - ONE task per call (step-based, never unbounded loops)
  - Autonomy mode governs which tasks require user confirmation (replaces hard HIGH guard)
  - All skill calls go through SkillExecutor (preserves PolicyEngine enforcement)
  - Cognitive budget enforced before execution; exceeded → goal paused
  - Stability: consecutive failures → backoff; failure signature recorded
  - Episodic memory written on task completion (budget-checked)
  - Observability events emitted on every state transition
  - Returns updated Goal + GoalEvent; caller persists to Redis
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from uuid import uuid4

import structlog

from ..db.session import async_session
from ..memory.manager import MemoryManager
from ..memory.types import MemoryType
from ..skills.executor import SkillExecutor
from ..skills.types import SkillCall
from .autonomy import autonomy_label, confirmation_reason, needs_confirmation
from .budget import (
    BudgetError,
    check_steps,
    mark_exceeded,
    record_execution_step,
    record_memory_write,
)
from .plan_validator import scan_arguments as _scan_placeholder
from .stability import (
    apply_backoff_if_needed,
    check_backoff,
    on_task_failure,
    on_task_success,
    record_intervention,
)
from .types import Goal, GoalEvent, GoalState, TaskNode, TaskStatus

logger = structlog.get_logger()

_STREAM_OUTGOING = "events:outgoing"


class StepResult:
    """Result of a single GoalStepExecutor.step() call."""

    __slots__ = ("goal", "event", "action")

    def __init__(self, goal: Goal, event: GoalEvent, action: str):
        self.goal = goal
        self.event = event
        self.action = action


class GoalStepExecutor:
    """Executes one step of a Goal's TaskGraph.

    Integrates autonomy mode, cognitive budget, and stability checks.
    Dependencies injected by GoalOrchestrator; no global state.
    """

    def __init__(
        self,
        skill_executor: SkillExecutor,
        memory_manager: MemoryManager,
        bus,
        default_chat_id: str = "",
    ):
        self.skill_executor = skill_executor
        self.memory_manager = memory_manager
        self.bus = bus
        self.default_chat_id = default_chat_id

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def step(self, goal: Goal) -> StepResult:
        """Execute the next ready task in the goal's TaskGraph.

        Returns a StepResult containing the updated Goal and a GoalEvent.
        The caller is responsible for persisting goal state to Redis.
        """
        now_iso = datetime.now(timezone.utc).isoformat()

        # ── Budget: already exceeded from a previous step ──────────────
        if goal.budget.budget_exceeded:
            return self._budget_exceeded_result(goal, now_iso)

        # ── Stability: currently in backoff ────────────────────────────
        in_backoff, backoff_reason = check_backoff(goal.stability)
        if in_backoff:
            event = GoalEvent(
                event_name="goal.stability_backoff",
                goal_id=goal.id,
                chat_id=goal.chat_id,
                detail=backoff_reason,
            )
            logger.info(
                "goal_executor.backoff",
                goal_id=goal.id,
                reason=backoff_reason,
            )
            return StepResult(goal, event, "stability_backoff")

        # ── Locked by stability ────────────────────────────────────────
        if goal.stability.locked:
            goal.state = GoalState.PAUSED
            event = GoalEvent(
                event_name="goal.paused",
                goal_id=goal.id,
                chat_id=goal.chat_id,
                detail="Stability: goal locked due to oscillation",
            )
            await self._notify(
                goal,
                "⚠️ Goal paused — stability lock detected (oscillating failure pattern).\n"
                f"_{goal.objective[:100]}_",
            )
            return StepResult(goal, event, "stability_locked")

        # ── Safety guard: step limit (hard limit on goal) ──────────────
        if goal.is_steps_exceeded():
            return self._fail_goal(
                goal,
                now_iso,
                f"Step limit reached ({goal.max_steps})",
                "limit_exceeded",
            )

        # ── Budget: step count ─────────────────────────────────────────
        try:
            check_steps(goal.budget)
        except BudgetError as e:
            return self._handle_budget_exceeded(goal, now_iso, e.dimension)

        # ── Safety guard: runtime limit ────────────────────────────────
        if goal.is_runtime_exceeded():
            return self._fail_goal(
                goal,
                now_iso,
                f"Runtime limit exceeded ({goal.max_runtime_seconds}s)",
                "limit_exceeded",
            )

        # ── Find next executable task ──────────────────────────────────
        task = goal.task_graph.get_next_task()

        if task is None:
            if goal.task_graph.has_permanently_failed():
                failed = goal.task_graph.get_failed_tasks()
                err = f"Tasks failed permanently: {', '.join(t.id for t in failed)}"
                return self._fail_goal(goal, now_iso, err, "goal_failed")

            if goal.task_graph.is_complete():
                goal.state = GoalState.COMPLETED
                goal.completed_at = now_iso
                goal.progress = 100.0
                event = GoalEvent(
                    event_name="goal.completed",
                    goal_id=goal.id,
                    chat_id=goal.chat_id,
                    detail=f"All {goal.task_graph.total_tasks} tasks completed",
                )
                logger.info(
                    "goal_executor.complete",
                    goal_id=goal.id,
                    steps=goal.step_count,
                )
                await self._notify(goal, self._build_completion_message(goal))
                return StepResult(goal, event, "goal_complete")

            event = GoalEvent(
                event_name="goal.waiting",
                goal_id=goal.id,
                chat_id=goal.chat_id,
                detail="No executable tasks (waiting for dependencies)",
            )
            return StepResult(goal, event, "no_task")

        # ── Autonomy check ─────────────────────────────────────────────
        if needs_confirmation(task, goal.autonomy_mode):
            reason = confirmation_reason(task, goal.autonomy_mode)
            label = autonomy_label(task, goal.autonomy_mode)

            task.status = TaskStatus.BLOCKED
            task.error = reason
            goal.state = GoalState.PAUSED

            # Telemetry
            goal.telemetry.autonomy_decisions[label] = (
                goal.telemetry.autonomy_decisions.get(label, 0) + 1
            )

            event = GoalEvent(
                event_name="goal.autonomy_blocked",
                goal_id=goal.id,
                task_id=task.id,
                chat_id=goal.chat_id,
                detail=reason,
            )
            logger.warning(
                "goal_executor.autonomy_blocked",
                goal_id=goal.id,
                task_id=task.id,
                skill=task.skill_name,
                mode=goal.autonomy_mode.value,
                risk=task.risk_level.value,
            )
            await self._notify(
                goal,
                f"⚠️ Goal paused — task requires confirmation ({goal.autonomy_mode.value} mode).\n"
                f"Task: *{task.description}*\n"
                f"Skill: `{task.skill_name}` | Risk: `{task.risk_level.value}`\n"
                f"Reason: {reason}\n"
                "Resume from the dashboard after reviewing.",
            )
            return StepResult(goal, event, "autonomy_blocked")

        # ── Agent capability sandbox (pre-PolicyEngine) ───────────────
        # If this goal belongs to an agent with an allowed_capabilities list,
        # verify the task's required_capability is in that list.
        if goal.allowed_capabilities and task.required_capability not in goal.allowed_capabilities:
            task.status = TaskStatus.BLOCKED
            task.error = (
                f"Agent sandbox: capability '{task.required_capability}' "
                f"not in allowed list {goal.allowed_capabilities}"
            )
            goal.state = GoalState.PAUSED
            goal.telemetry.policy_blocks += 1
            event = GoalEvent(
                event_name="goal.sandbox_denied",
                goal_id=goal.id,
                task_id=task.id,
                chat_id=goal.chat_id,
                detail=task.error,
            )
            logger.warning(
                "goal_executor.sandbox_denied",
                goal_id=goal.id,
                task_id=task.id,
                capability=task.required_capability,
                allowed=goal.allowed_capabilities,
                agent_id=goal.agent_id,
            )
            return StepResult(goal, event, "sandbox_denied")

        # ── Intent boundary gate (goal-driven side-effect skills) ─────
        # Goal-driven calls go through the SAME intent_gate_check as chat
        # paths — full validation: explicit keyword AND (for gmail) recipient
        # grounded in the goal objective AND content not placeholder. This
        # closes the bypass where a goal's gmail.send could pass the regex
        # but invent a recipient ("contact@example.com") or placeholder
        # body. Single source of truth across all paths.
        try:
            from ..policy import intent_gate_check as _policy_check
            _SIDE_EFFECT_ACTIONS = {
                "agent_manager": "create",
                "task_manager":  "create",
                "gmail":         "send",
            }
            _action_arg = ""
            if isinstance(task.arguments, dict):
                _action_arg = str(task.arguments.get("action", "")).lower().strip()
            _required_action = _SIDE_EFFECT_ACTIONS.get(task.skill_name)
            if _required_action and _action_arg in (_required_action, "draft"):
                # Wrap the goal task as a duck-typed SkillCall for the gate.
                class _GoalTaskAsCall:
                    skill_name = task.skill_name
                    arguments = task.arguments if isinstance(task.arguments, dict) else {}
                _ok, _gate_reason, _gate_label = _policy_check(
                    _GoalTaskAsCall(),
                    goal.objective or "",
                    ctx_messages=None,
                    chat_id=str(goal.chat_id or ""),
                    recent_action_resolver=None,
                )
                if not _ok:
                    now_iso = datetime.now(timezone.utc).isoformat()
                    task.retries = task.max_retries
                    task.status = TaskStatus.FAILED
                    task.error = (
                        f"Intent gate (goal): {task.skill_name}({_action_arg}) blocked — "
                        f"reason={_gate_reason}. The goal objective does not authorize this "
                        f"side-effect (no explicit keyword OR missing recipient OR placeholder content)."
                    )
                    task.completed_at = now_iso
                    on_task_failure(goal.stability)
                    apply_backoff_if_needed(goal.stability)
                    event = GoalEvent(
                        event_name="task.failed",
                        goal_id=goal.id,
                        task_id=task.id,
                        chat_id=goal.chat_id,
                        detail=task.error,
                    )
                    logger.warning(
                        "goal_executor.intent_gate_blocked",
                        goal_id=goal.id,
                        task_id=task.id,
                        skill=task.skill_name,
                        action=_action_arg,
                        reason=_gate_reason,
                        label=_gate_label,
                    )
                    return StepResult(goal, event, "task_failed")
        except Exception:
            logger.exception("goal_executor.intent_gate_error")  # Don't break execution; surface in logs

        # ── Placeholder guard (BEFORE execution) ───────────────────────
        # Reject tasks whose arguments contain obvious template / hallucinated
        # values like "/path/to/file", "<api_key>", "TODO". On hit, force a
        # permanent failure so the existing replan path (capped by
        # MAX_REPLAN_COUNT + budget.max_replans) handles recovery cleanly.
        _ph_hit = _scan_placeholder(task.arguments)
        if _ph_hit:
            _ph_field, _ph_value, _ph_pattern = _ph_hit
            now_iso = datetime.now(timezone.utc).isoformat()
            task.retries = task.max_retries
            task.status = TaskStatus.FAILED
            task.error = (
                f"Invalid placeholder value detected in task arguments: "
                f"{_ph_field}={_ph_value!r} (pattern: {_ph_pattern})"
            )
            task.completed_at = now_iso
            on_task_failure(goal.stability)
            apply_backoff_if_needed(goal.stability)
            event = GoalEvent(
                event_name="task.failed",
                goal_id=goal.id,
                task_id=task.id,
                chat_id=goal.chat_id,
                detail=task.error,
            )
            logger.warning(
                "goal_executor.placeholder_rejected",
                goal_id=goal.id,
                task_id=task.id,
                skill=task.skill_name,
                field=_ph_field,
                pattern=_ph_pattern,
            )
            return StepResult(goal, event, "task_failed")

        # ── Record autonomy decision (auto) ────────────────────────────
        goal.telemetry.autonomy_decisions["auto"] = (
            goal.telemetry.autonomy_decisions.get("auto", 0) + 1
        )
        # Track skill distribution
        goal.telemetry.skill_distribution[task.skill_name] = (
            goal.telemetry.skill_distribution.get(task.skill_name, 0) + 1
        )

        # ── Execute the task ───────────────────────────────────────────
        return await self._execute_task(goal, task)

    # ------------------------------------------------------------------
    # Internal: task execution
    # ------------------------------------------------------------------

    async def _execute_task(self, goal: Goal, task: TaskNode) -> StepResult:
        now_iso = datetime.now(timezone.utc).isoformat()

        task.status = TaskStatus.RUNNING
        task.started_at = now_iso
        goal.step_count += 1
        record_execution_step(goal.budget)

        logger.info(
            "goal_executor.task_start",
            goal_id=goal.id,
            task_id=task.id,
            skill=task.skill_name,
            step=goal.step_count,
            autonomy=goal.autonomy_mode.value,
        )

        t0 = time.monotonic()
        call_args = dict(task.arguments)
        # Inject agent context into create_reminder so recurring goals restart automatically
        if task.skill_name == "create_reminder" and getattr(goal, "agent_id", ""):
            call_args.setdefault("agent_id", goal.agent_id)
            call_args.setdefault("agent_objective", goal.objective)
        result = await self.skill_executor.execute(
            SkillCall(skill_name=task.skill_name, arguments=call_args),
            user_id=goal.user_id or "goal_engine",
            chat_id=goal.chat_id or self.default_chat_id,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)

        task.completed_at = datetime.now(timezone.utc).isoformat()

        if result.success:
            task.status = TaskStatus.DONE
            task.output_summary = (result.output or "")[:500]
            goal.progress = goal.task_graph.compute_progress()

            # Stability: reset consecutive failures on success
            on_task_success(goal.stability)

            # Stamp recent_action so a chat-side "haz lo mismo" follow-up
            # can resolve to this goal-driven side-effect. Bug #11 — without
            # this, an email sent via goal_orchestrator left no breadcrumb
            # for the chat handler's reference-phrase tracker.
            try:
                _SE_SKILLS = {"gmail", "agent_manager", "task_manager"}
                if task.skill_name in _SE_SKILLS:
                    from ..events.handlers import _record_explicit_action
                    _action_arg = ""
                    _recip = ""
                    if isinstance(task.arguments, dict):
                        _action_arg = str(task.arguments.get("action", "")).lower().strip()
                        _recip = str(task.arguments.get("to", "") or "")
                    _record_explicit_action(
                        chat_id=str(goal.chat_id or ""),
                        skill_name=task.skill_name,
                        action=_action_arg,
                        recipient=_recip,
                    )
            except Exception as _stamp_err:
                logger.debug("goal_executor.stamp_recent_action_failed", error=str(_stamp_err)[:80])

            event = GoalEvent(
                event_name="task.completed",
                goal_id=goal.id,
                task_id=task.id,
                chat_id=goal.chat_id,
                latency_ms=latency_ms,
                detail=task.output_summary,
            )
            logger.info(
                "goal_executor.task_done",
                goal_id=goal.id,
                task_id=task.id,
                skill=task.skill_name,
                ms=latency_ms,
                progress=goal.progress,
            )
            await self._write_episodic_memory(goal, task, result.output, success=True)
            # Goal-scoped observation — distilled record of what worked for this goal.
            # Importance moderate (0.55) — successes are useful but failures matter more.
            try:
                from ..memory.goal_memory import add_observation as _gm_add
                _summary = (task.output_summary or "")[:240]
                _obs = (
                    f"OK [{task.skill_name}] {task.description[:80]}"
                    + (f" → {_summary}" if _summary else "")
                )
                await _gm_add(goal_id=goal.id, observation=_obs, importance=0.55)
            except Exception as _gm_err:
                logger.debug("goal_memory.task_done_record_failed", error=str(_gm_err)[:80])
            return StepResult(goal, event, "task_done")

        else:
            # Task failed
            task.retries += 1
            task.error = (result.error or "")[:300]

            if task.retries < task.max_retries:
                # Retry: reset to PENDING
                task.status = TaskStatus.PENDING
                task.started_at = None
                task.completed_at = None
                # Increment consecutive failures (not permanent yet)
                on_task_failure(goal.stability)
                applied = apply_backoff_if_needed(goal.stability)
                if applied:
                    record_intervention(
                        goal.stability,
                        f"Backoff: {goal.stability.consecutive_failures} consecutive failures",
                    )
                    goal.telemetry.stability_interventions += 1
                    logger.warning(
                        "goal_executor.backoff_applied",
                        goal_id=goal.id,
                        consecutive=goal.stability.consecutive_failures,
                        backoff_until=goal.stability.backoff_until,
                    )

                event = GoalEvent(
                    event_name="task.failed",
                    goal_id=goal.id,
                    task_id=task.id,
                    chat_id=goal.chat_id,
                    latency_ms=latency_ms,
                    detail=f"Retry {task.retries}/{task.max_retries}: {task.error}",
                )
                logger.warning(
                    "goal_executor.task_retry",
                    goal_id=goal.id,
                    task_id=task.id,
                    retry=task.retries,
                    max_retries=task.max_retries,
                    error=task.error,
                )
                return StepResult(goal, event, "task_retrying")

            else:
                # Permanent failure
                task.status = TaskStatus.FAILED
                on_task_failure(goal.stability)
                apply_backoff_if_needed(goal.stability)

                event = GoalEvent(
                    event_name="task.failed",
                    goal_id=goal.id,
                    task_id=task.id,
                    chat_id=goal.chat_id,
                    latency_ms=latency_ms,
                    detail=f"Permanent failure after {task.retries} retries: {task.error}",
                )
                logger.error(
                    "goal_executor.task_permanent_failure",
                    goal_id=goal.id,
                    task_id=task.id,
                    skill=task.skill_name,
                    error=task.error,
                )
                await self._write_episodic_memory(goal, task, task.error, success=False)
                # Goal-scoped observation — distilled failure record. Failures
                # weighted higher (0.85) because they teach more than successes.
                try:
                    from ..memory.goal_memory import add_observation as _gm_add
                    _err = (task.error or "unknown")[:240]
                    _obs = f"FAIL [{task.skill_name}] {task.description[:80]} → {_err}"
                    await _gm_add(goal_id=goal.id, observation=_obs, importance=0.85)
                except Exception as _gm_err:
                    logger.debug("goal_memory.task_failed_record_failed", error=str(_gm_err)[:80])
                return StepResult(goal, event, "task_failed")

    # ------------------------------------------------------------------
    # Error state helpers
    # ------------------------------------------------------------------

    def _fail_goal(
        self, goal: Goal, now_iso: str, error: str, action: str
    ) -> StepResult:
        goal.state = GoalState.FAILED
        goal.completed_at = now_iso
        goal.error = error
        event = GoalEvent(
            event_name="goal.failed",
            goal_id=goal.id,
            chat_id=goal.chat_id,
            detail=error,
        )
        logger.warning("goal_executor.goal_failed", goal_id=goal.id, error=error)
        # Fire-and-forget notify — schedule on running loop from this sync method
        import asyncio
        try:
            _title = goal.title or goal.objective[:60]
            asyncio.get_running_loop().create_task(
                self._notify(goal, f"❌ *{_title}*\n{error[:120]}")
            )
        except RuntimeError:
            pass  # no running loop (tests / offline)
        return StepResult(goal, event, action)

    def _budget_exceeded_result(self, goal: Goal, now_iso: str) -> StepResult:
        goal.state = GoalState.PAUSED
        event = GoalEvent(
            event_name="goal.budget_exceeded",
            goal_id=goal.id,
            chat_id=goal.chat_id,
            detail=f"Budget exceeded: {goal.budget.budget_exceeded_dimension}",
        )
        return StepResult(goal, event, "budget_exceeded")

    def _handle_budget_exceeded(
        self, goal: Goal, now_iso: str, dimension: str
    ) -> StepResult:
        mark_exceeded(goal.budget, dimension)
        goal.state = GoalState.PAUSED
        goal.telemetry.budget_exceeded_events += 1
        event = GoalEvent(
            event_name="goal.budget_exceeded",
            goal_id=goal.id,
            chat_id=goal.chat_id,
            detail=f"Budget exceeded: {dimension}",
        )
        logger.warning(
            "goal_executor.budget_exceeded",
            goal_id=goal.id,
            dimension=dimension,
        )
        import asyncio
        try:
            asyncio.get_running_loop().create_task(
                self._notify(
                    goal,
                    f"⚠️ Goal paused — cognitive budget exceeded: *{dimension}*\n"
                    f"_{goal.objective[:100]}_\n"
                    "Resume from the dashboard after reviewing budget limits.",
                )
            )
        except RuntimeError:
            pass
        return StepResult(goal, event, "budget_exceeded")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _write_episodic_memory(
        self,
        goal: Goal,
        task: TaskNode,
        output: str,
        success: bool,
    ) -> None:
        """Write task result to episodic memory (budget-checked)."""
        content = {
            "goal_id": goal.id,
            "goal_objective": goal.objective[:200],
            "task_id": task.id,
            "task_description": task.description[:200],
            "skill_name": task.skill_name,
            "output": output[:500],
            "success": success,
            "step_count": goal.step_count,
        }
        try:
            async with async_session() as session:
                await self.memory_manager.store_memory(
                    session=session,
                    memory_type=MemoryType.EPISODIC,
                    content=content,
                    summary=f"[Goal task] {task.description[:100]}",
                    tags=["goal", "autonomous", task.skill_name],
                    source="goal_engine",
                )
            # Update memory budget counter (best-effort; doesn't block on error)
            record_memory_write(goal.budget, content)
        except Exception:
            logger.exception(
                "goal_executor.memory_write_error",
                goal_id=goal.id,
                task_id=task.id,
            )

    def _build_completion_message(self, goal: Goal) -> str:
        """Build a clean natural-language completion message from task results."""
        import re as _re
        _strip_emoji = _re.compile(r"^[\s✅⚠️❌🔔📧📩➡️▶️•\-]+")

        first_lines = []
        for node in goal.task_graph.nodes:
            if node.status.value != "done":
                continue
            summary = (node.output_summary or "").strip()
            if not summary:
                continue
            # Take only the first meaningful line — skip ID/status/description detail lines
            for raw_line in summary.split("\n"):
                line = _strip_emoji.sub("", raw_line).strip()
                if len(line) >= 15 and not line.lower().startswith(("id:", "status:", "description:", "autonomy:", "▶️", "goal started")):
                    first_lines.append(line)
                    break

        if first_lines:
            return "✅ " + " | ".join(first_lines[:3])
        else:
            title = goal.title or goal.objective[:80]
            return f"✅ Listo: {title}"

    async def _notify(self, goal: Goal, text: str) -> None:
        """Send a Telegram notification for this goal (best-effort)."""
        chat_id = goal.chat_id or self.default_chat_id
        if not chat_id:
            return
        # Strip any markdown/execution-steps artifacts before sending
        try:
            from ..events.handlers import _clean_telegram_output
            text = _clean_telegram_output(text)
        except Exception:
            pass
        if not text:
            return
        try:
            await self.bus.publish(
                _STREAM_OUTGOING,
                {
                    "event_type": "telegram.response",
                    "chat_id": chat_id,
                    "text": text,
                    "correlation_id": str(uuid4()),
                    "metadata": "{}",
                },
            )
        except Exception:
            logger.exception("goal_executor.notify_error", goal_id=goal.id)
