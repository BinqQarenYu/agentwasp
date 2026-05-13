"""GoalOrchestrator — top-level coordinator for the Autonomous Goal Engine.

Advanced Control Layer additions (layered, non-breaking):
  - Cognitive budget enforcement: tracks and limits planning tokens + replans
  - Autonomy mode: per-goal task confirmation policy
  - Stability: replan storm detection; pause on consecutive storm
  - Template support: create from template, update stats on completion
  - CPU backpressure: skip tick if system is overloaded
  - All new state is serialized in the Goal model (Redis-persisted)

Design invariants preserved:
  - All skill execution via SkillExecutor
  - One task per goal per tick
  - Concurrency limit enforced
  - No unbounded loops
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import redis.asyncio as aioredis
import structlog

from ..memory.manager import MemoryManager
from ..skills.executor import SkillExecutor
from .budget import (
    BudgetError,
    check_planning_tokens,
    check_replan,
    mark_exceeded,
    record_planning_tokens,
    record_replan,
)
from .executor import GoalStepExecutor
from .planner import PlanGenerator
from .stability import record_intervention, record_replan as stability_record_replan
from .events import emit_goal_event
from .store import cleanup_old_goals, list_active_goals, load_goal, save_goal
from .templates import GoalTemplate, load_template, update_template_stats
from .types import (
    AutonomyMode,
    CognitiveBudget,
    Goal,
    GoalEvent,
    GoalState,
    TaskNode,
    TaskStatus,
)

logger = structlog.get_logger()

MAX_REPLAN_COUNT = 5   # Hard cap (loosened from 3 — complex multi-skill goals
                       # legitimately need 4 replans when sources block).
MAX_PLAN_STEPS = 8     # Plans larger than this are split / truncated for stability


def _keyword_overlap(a: str, b: str) -> float:
    """Return word-level Jaccard overlap between two strings (0.0–1.0)."""
    _stop = {"the", "a", "an", "to", "of", "and", "or", "in", "for", "with", "que",
             "de", "la", "el", "en", "y", "un", "una", "los", "las", "por", "con"}
    words_a = {w.lower() for w in a.split() if len(w) > 2 and w.lower() not in _stop}
    words_b = {w.lower() for w in b.split() if len(w) > 2 and w.lower() not in _stop}
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


def _extract_title(objective: str) -> str:
    """Generate a short display title (≤50 chars) from a goal objective."""
    import re
    MAX = 50
    # Scheduled task: use task name from prefix
    m = re.match(r"\[TAREA PROGRAMADA:\s*([^\]]+)\]", objective)
    if m:
        t = m.group(1).strip()
        return t if len(t) <= MAX else t[:MAX - 1].rsplit(" ", 1)[0] + "…"
    # Strip common preamble phrases
    clean = objective.strip()
    clean = re.sub(r"^(?:objetivo|goal|tarea|instrucciones?)[\s:\-]+", "", clean, flags=re.I)
    clean = re.sub(r"^(?:necesito que hagas esto|quiero que resuelvas esto|quiero que|necesito que|por favor)[^:]*:?\s*", "", clean, flags=re.I)
    # Use first non-empty meaningful line
    for line in clean.splitlines():
        line = line.strip()
        if len(line) >= 5:
            if len(line) <= MAX:
                return line
            # Cut at word boundary
            cut = line[:MAX - 1].rsplit(" ", 1)[0]
            return cut + "…"
    t = clean[:MAX - 1].rsplit(" ", 1)[0] + "…" if len(clean) > MAX else clean
    return t or objective[:MAX]


class GoalOrchestrator:
    """Coordinates goal lifecycle: create → plan → execute → complete/fail.

    Wired up in main.py; passed to GoalTickJob and dashboard routes.
    """

    def __init__(
        self,
        redis_url: str,
        plan_generator: PlanGenerator,
        skill_executor: SkillExecutor,
        memory_manager: MemoryManager,
        bus,
        max_concurrent: int = 3,
        default_chat_id: str = "",
        # Budget defaults (override via settings)
        budget_max_tokens_planning: int = 2000,
        budget_max_tokens_execution: int = 10000,
        budget_max_replans: int = 3,
        budget_max_memory_bytes: int = 1_048_576,
        default_autonomy_mode: AutonomyMode = AutonomyMode.SEMI,
    ):
        self.redis_url = redis_url
        self.plan_generator = plan_generator
        self.skill_executor = skill_executor
        self.memory_manager = memory_manager
        self.bus = bus
        self.max_concurrent = max_concurrent
        self.default_chat_id = default_chat_id
        self.budget_max_tokens_planning = budget_max_tokens_planning
        self.budget_max_tokens_execution = budget_max_tokens_execution
        self.budget_max_replans = budget_max_replans
        self.budget_max_memory_bytes = budget_max_memory_bytes
        self.default_autonomy_mode = default_autonomy_mode
        # Optional plan critic (System 2 — Dual-Layer Planner)
        self.plan_critic = None  # Set via late-wiring in main.py when PLAN_CRITIC_ENABLED=true
        # Optional resource governor (late-wired from main.py)
        self.governor = None
        # Optional self-reflection engine (late-wired from main.py)
        self.reflection_engine = None
        # Optional capability evolution engine (late-wired from main.py)
        self.capability_evolution_engine = None
        # Execution backend — defaults to LocalExecutionBackend (same-process).
        # Can be replaced with QueueExecutionBackend for distributed workers.
        # Late-wired from main.py after the orchestrator is created.
        from .execution_backend import LocalExecutionBackend
        self.execution_backend = LocalExecutionBackend()
        self.step_executor = GoalStepExecutor(
            skill_executor=skill_executor,
            memory_manager=memory_manager,
            bus=bus,
            default_chat_id=default_chat_id,
        )

    # ------------------------------------------------------------------
    # Goal creation
    # ------------------------------------------------------------------

    async def create_goal(
        self,
        objective: str,
        chat_id: str = "",
        user_id: str = "",
        constraints: str = "",
        success_criteria: str = "",
        max_steps: int = 50,
        max_runtime_seconds: int = 3600,
        autonomy_mode: AutonomyMode | None = None,
        template_id: str | None = None,
        # Per-goal budget overrides (None = use system defaults)
        budget_max_tokens_planning: int | None = None,
        budget_max_replans: int | None = None,
        # Priority arbitration: 1 (lowest) – 10 (highest). Default 5.
        # User-interactive goals should use 8; autonomous background goals 3.
        priority: int = 5,
        source: str = "user",
    ) -> Goal:
        """Create, plan, and activate a new goal.

        If template_id is provided the existing TaskGraph is reused and
        the LLM planning call is skipped entirely.

        Returns the Goal in ACTIVE state on success, FAILED on planning error.
        """
        mode = autonomy_mode if autonomy_mode is not None else self.default_autonomy_mode

        budget = CognitiveBudget(
            max_tokens_planning=budget_max_tokens_planning or self.budget_max_tokens_planning,
            max_tokens_execution=self.budget_max_tokens_execution,
            max_replans=budget_max_replans or self.budget_max_replans,
            max_steps=max_steps,
            max_memory_growth_bytes=self.budget_max_memory_bytes,
            max_runtime_seconds=max_runtime_seconds,
        )

        goal = Goal(
            id=str(uuid4()),
            objective=objective,
            title=_extract_title(objective),
            chat_id=chat_id,
            user_id=user_id,
            constraints=constraints,
            success_criteria=success_criteria,
            max_steps=max_steps,
            max_runtime_seconds=max_runtime_seconds,
            state=GoalState.PLANNING,
            autonomy_mode=mode,
            budget=budget,
            priority=priority,
            source=source,
        )

        r = await self._redis()
        try:
            # ── Resource Governor check ────────────────────────────────────────
            if self.governor:
                _gov_ok, _gov_msg = await self.governor.check_allow("create_goal", user_id=user_id)
                if not _gov_ok:
                    goal.state = GoalState.FAILED
                    goal.error = _gov_msg
                    logger.warning("goal_orchestrator.governor_blocked", user_id=user_id, reason=_gov_msg)
                    return goal

            # ── Objective 3: Duplicate goal detection ─────────────────────────
            try:
                active = await list_active_goals(r)
                for existing in active:
                    overlap = _keyword_overlap(objective, existing.objective)
                    if overlap >= 0.60:
                        logger.warning(
                            "goal_orchestrator.duplicate_rejected",
                            existing_id=existing.id,
                            overlap=round(overlap, 2),
                            objective=objective[:80],
                        )
                        # Return the existing goal instead of creating a duplicate
                        return existing
            except Exception:
                pass  # Duplicate check never blocks goal creation

            await save_goal(r, goal)
            # Event sourcing — GoalCreated
            asyncio.get_running_loop().create_task(emit_goal_event(
                self.redis_url, "GoalCreated", goal.id,
                state=goal.state.value, objective=goal.objective[:100],
                source=goal.source or "",
            ))
            if self.governor:
                await self.governor.record_action("create_goal", user_id=user_id)
            logger.info(
                "goal_orchestrator.plan_created",
                goal_id=goal.id,
                objective=objective[:80],
                autonomy=mode.value,
                template_id=template_id,
                source=source,
            )
            await self._emit(GoalEvent(
                event_name="goal.created",
                goal_id=goal.id,
                chat_id=chat_id,
                detail=objective[:200],
            ))

            # ── Template path ──────────────────────────────────────────
            if template_id:
                template = await load_template(r, template_id)
                if template:
                    graph = template.load_task_graph()
                    goal.task_graph = graph
                    goal.state = GoalState.ACTIVE
                    goal.started_at = datetime.now(timezone.utc).isoformat()
                    goal.telemetry.template_id = template_id
                    goal.telemetry.template_name = template.name
                    await save_goal(r, goal)
                    await self._emit(GoalEvent(
                        event_name="goal.planned",
                        goal_id=goal.id,
                        chat_id=chat_id,
                        detail=f"Template '{template.name}': {graph.total_tasks} tasks",
                    ))
                    logger.info(
                        "goal_orchestrator.from_template",
                        goal_id=goal.id,
                        template_id=template_id,
                        tasks=graph.total_tasks,
                    )
                    return goal
                else:
                    logger.warning(
                        "goal_orchestrator.template_not_found",
                        template_id=template_id,
                        goal_id=goal.id,
                    )
                    # Fall through to normal planning

            # ── Budget: planning tokens check ──────────────────────────
            try:
                check_planning_tokens(goal.budget)
            except BudgetError as e:
                goal.state = GoalState.FAILED
                goal.error = f"Budget exceeded before planning: {e}"
                goal.completed_at = datetime.now(timezone.utc).isoformat()
                await save_goal(r, goal)
                logger.error("goal_orchestrator.budget_before_plan", goal_id=goal.id)
                return goal

            # ── Normal planning path ───────────────────────────────────
            graph, error, tokens_used = await self.plan_generator.generate(goal)
            record_planning_tokens(goal.budget, tokens_used)

            if graph is None:
                goal.state = GoalState.FAILED
                goal.error = f"Planning failed: {error}"
                goal.completed_at = datetime.now(timezone.utc).isoformat()
                await save_goal(r, goal)
                logger.error("goal_orchestrator.plan_failed", goal_id=goal.id, error=error)
                await self._emit(GoalEvent(
                    event_name="goal.failed",
                    goal_id=goal.id,
                    chat_id=chat_id,
                    detail=goal.error,
                ))
                return goal

            # ── System 2: Plan Critic (optional second LLM validation pass) ──
            if self.plan_critic is not None:
                try:
                    graph = await self.plan_critic.validate(goal, graph)
                except Exception as _critic_exc:
                    logger.warning(
                        "goal_orchestrator.critic_error",
                        goal_id=goal.id,
                        error=str(_critic_exc)[:80],
                    )
                    # Fall back silently to original graph

            # ── Objective 2 Safeguard 2: Cap plan at MAX_PLAN_STEPS ────────
            if graph.total_tasks > MAX_PLAN_STEPS:
                logger.warning(
                    "goal_orchestrator.plan_truncated",
                    goal_id=goal.id,
                    original_tasks=graph.total_tasks,
                    capped_at=MAX_PLAN_STEPS,
                )
                # Keep first MAX_PLAN_STEPS nodes (topological order preserved by planner)
                graph.nodes = graph.nodes[:MAX_PLAN_STEPS]

            goal.task_graph = graph
            goal.state = GoalState.ACTIVE
            goal.started_at = datetime.now(timezone.utc).isoformat()
            await save_goal(r, goal)
            # Event sourcing — GoalStarted
            asyncio.get_running_loop().create_task(emit_goal_event(
                self.redis_url, "GoalStarted", goal.id,
                state="active", task_count=str(graph.total_tasks),
            ))

            await self._emit(GoalEvent(
                event_name="goal.planned",
                goal_id=goal.id,
                chat_id=chat_id,
                detail=f"Planned {graph.total_tasks} tasks ({tokens_used} tokens)",
            ))
            logger.info(
                "goal_orchestrator.plan_created",
                plan_type="plan_created",
                goal_id=goal.id,
                tasks=graph.total_tasks,
                tokens_used=tokens_used,
                source=source,
            )
            return goal

        finally:
            await r.aclose()

    # ------------------------------------------------------------------
    # Tick — called by GoalTickJob
    # ------------------------------------------------------------------

    async def tick(self) -> str:
        """Advance one step for each active goal (respecting concurrency limit).

        Includes CPU backpressure: if system CPU > 85%, defer execution.
        Returns a summary string for logging.
        """
        if _cpu_overloaded():
            logger.info("goal_orchestrator.cpu_backpressure_skipped")
            return "CPU overloaded — tick skipped"

        r = await self._redis()
        try:
            active_goals = await list_active_goals(r)
        finally:
            await r.aclose()

        if not active_goals:
            return "0 active goals"

        # Sort by priority descending (highest first) so user-interactive goals
        # are always processed before background autonomous goals when slots are limited.
        active_goals.sort(key=lambda g: g.priority, reverse=True)
        goals_to_process = active_goals[: self.max_concurrent]

        results = await asyncio.gather(
            *[self._tick_one(g) for g in goals_to_process],
            return_exceptions=True,
        )

        actions: list[str] = []
        for goal, res in zip(goals_to_process, results):
            if isinstance(res, Exception):
                logger.exception(
                    "goal_orchestrator.tick_error", goal_id=goal.id, exc=str(res)
                )
                actions.append(f"{goal.id[:8]}=error")
            else:
                actions.append(f"{goal.id[:8]}={res}")

        r = await self._redis()
        try:
            await cleanup_old_goals(r)
        except Exception:
            pass
        finally:
            await r.aclose()

        return f"Ticked {len(goals_to_process)} goals: {', '.join(actions)}"

    async def _tick_one(self, goal: Goal) -> str:
        """Execute up to 3 consecutive steps for a single goal per tick.

        Multi-step execution dramatically reduces latency for goal-driven tasks:
        instead of waiting for the next 15s tick between each step, we chain
        up to 3 steps in a single tick invocation. We stop early if the goal
        leaves ACTIVE state or enters a waiting condition.
        Returns comma-separated action string (e.g. "planned,task_done,completed").
        """
        MAX_STEPS_PER_TICK = 3
        actions: list[str] = []
        current_goal = goal

        for _step_idx in range(MAX_STEPS_PER_TICK):
            r = await self._redis()
            try:
                fresh_goal = await load_goal(r, current_goal.id)
                if fresh_goal is None:
                    return "missing" if not actions else ",".join(actions) + ";missing"
                if fresh_goal.state != GoalState.ACTIVE:
                    if actions:
                        return ",".join(actions) + f";{fresh_goal.state.value}"
                    return f"skip:{fresh_goal.state.value}"

                step_result = await self.step_executor.step(fresh_goal)
                updated_goal = step_result.goal

                # ── Objective 2 Safeguard 1: Plan Lock ───────────────────────
                # After first task success, lock the plan to prevent spurious replanning
                if step_result.action == "task_done" and not updated_goal.plan_locked:
                    updated_goal.plan_locked = True
                    logger.info(
                        "goal_orchestrator.plan_locked",
                        goal_id=updated_goal.id,
                        step_count=updated_goal.step_count,
                    )
                    await self._emit(GoalEvent(
                        event_name="goal.plan_locked",
                        goal_id=updated_goal.id,
                        chat_id=updated_goal.chat_id,
                        detail="Plan locked after first successful task",
                    ))

                # ── Objective 4: plan_completed event ────────────────────────
                if step_result.action in ("completed",) or updated_goal.state == GoalState.COMPLETED:
                    logger.info(
                        "goal_orchestrator.plan_completed",
                        goal_id=updated_goal.id,
                        step_count=updated_goal.step_count,
                        replan_count=updated_goal.replan_count,
                    )

                # Handle replanning on permanent task failure
                if step_result.action == "task_failed":
                    updated_goal = await self._try_replan(updated_goal, r)

                # Update template stats on completion or failure
                if updated_goal.state in (GoalState.COMPLETED, GoalState.FAILED):
                    if self.governor:
                        try:
                            await self.governor.release_slot("create_goal", user_id=updated_goal.user_id or "")
                        except Exception:
                            pass
                    # Self-Reflection Engine — fire-and-forget on completion/failure
                    if self.reflection_engine:
                        try:
                            import asyncio as _re_asyncio
                            outcome = "success" if updated_goal.state == GoalState.COMPLETED else "failure"
                            task_sums = [
                                f"{t.skill_name}: {(t.output_summary or '')[:100]}"
                                for t in updated_goal.task_graph.nodes
                                if t.status.value in ("done", "failed")
                            ]
                            _re_asyncio.get_running_loop().create_task(
                                self.reflection_engine.reflect_on_goal(
                                    goal_id=updated_goal.id,
                                    objective=updated_goal.objective,
                                    outcome=outcome,
                                    error=updated_goal.error or "",
                                    task_summaries=task_sums,
                                )
                            )
                        except Exception:
                            pass
                    # Capability Evolution Engine — fire-and-forget on goal failure
                    if self.capability_evolution_engine and updated_goal.state == GoalState.FAILED:
                        try:
                            import asyncio as _cee_asyncio
                            _cee_asyncio.get_running_loop().create_task(
                                self.capability_evolution_engine.analyze_gap(
                                    goal_id=updated_goal.id,
                                    objective=updated_goal.objective,
                                    outcome="failure",
                                    error=updated_goal.error or "",
                                    consecutive_failures=updated_goal.stability.consecutive_failures,
                                )
                            )
                        except Exception:
                            pass
                    if updated_goal.telemetry.template_id:
                        elapsed = updated_goal.runtime_seconds()
                        success = updated_goal.state == GoalState.COMPLETED
                        try:
                            await update_template_stats(
                                r,
                                updated_goal.telemetry.template_id,
                                success=success,
                                completion_seconds=elapsed,
                            )
                        except Exception:
                            pass

                await save_goal(r, updated_goal)
                # Event sourcing — emit per state transition and task events
                _action = step_result.action
                if updated_goal.state == GoalState.COMPLETED:
                    asyncio.get_running_loop().create_task(emit_goal_event(
                        self.redis_url, "GoalCompleted", updated_goal.id,
                        state="completed", steps=str(updated_goal.step_count),
                    ))
                elif updated_goal.state == GoalState.FAILED:
                    asyncio.get_running_loop().create_task(emit_goal_event(
                        self.redis_url, "GoalFailed", updated_goal.id,
                        state="failed", error=(updated_goal.error or "")[:120],
                    ))
                    # User-visible failure notification — without this the
                    # complex_directive_fast_path goes silent for minutes
                    # (replan storm → FAILED → no message). Lang heuristic:
                    # default ES (most users), but switch to EN if the
                    # objective is clearly English.
                    _err_text = (updated_goal.error or "Objective could not be completed.")[:200]
                    _objective = (updated_goal.objective or "")
                    _is_en = bool(_objective) and not any(
                        ch in _objective.lower() for ch in ("ñ", "á", "é", "í", "ó", "ú", "que ", " es ", " la ", " el ")
                    )
                    if _is_en:
                        _msg = f"⚠️ I could not complete that request: {_err_text}. Want me to try a smaller version?"
                    else:
                        _msg = f"⚠️ No pude completar esa solicitud: {_err_text}. ¿Quieres que lo intente de forma más simple?"
                    try:
                        await self._notify_simple(updated_goal, _msg)
                    except Exception:
                        logger.exception("goal_orchestrator.failure_notify_error", goal_id=updated_goal.id)
                elif _action in ("task_done", "task_failed"):
                    _task_id = step_result.event.task_id or ""
                    asyncio.get_running_loop().create_task(emit_goal_event(
                        self.redis_url, "TaskCompleted", updated_goal.id,
                        task_id=_task_id, task_status=_action,
                    ))
                elif _action == "task_started":
                    _task_id = step_result.event.task_id or ""
                    asyncio.get_running_loop().create_task(emit_goal_event(
                        self.redis_url, "TaskStarted", updated_goal.id,
                        task_id=_task_id,
                    ))
                await self._emit(step_result.event)
                actions.append(step_result.action)
                current_goal = updated_goal

                # Stop chaining if goal is no longer active
                if updated_goal.state != GoalState.ACTIVE:
                    break
                # Stop chaining if step caused a pause/block (state check above covers most cases)
                if step_result.action in ("autonomy_blocked", "sandbox_denied", "budget_exceeded"):
                    break

            except Exception:
                logger.exception("goal_orchestrator.tick_one_error", goal_id=current_goal.id)
                actions.append("error")
                break
            finally:
                await r.aclose()

        return ",".join(actions) if actions else "no_action"

    # ------------------------------------------------------------------
    # Replanning
    # ------------------------------------------------------------------

    async def _try_replan(self, goal: Goal, r) -> Goal:
        """Attempt to replan a goal that has a permanently failed task.

        Enforces budget.max_replans and stability storm detection.
        """
        now_iso = datetime.now(timezone.utc).isoformat()

        # ── Stability: replan storm detection ─────────────────────────
        storm = stability_record_replan(goal.stability)
        if storm:
            # Freeze goal with partial completion — don't leave user without feedback
            goal.state = GoalState.FAILED
            # Collect any completed task outputs for partial result
            _completed_outputs = [
                n.output_summary for n in goal.task_graph.nodes
                if n.status == TaskStatus.DONE and n.output_summary
            ] if goal.task_graph else []
            _partial = (
                "Partial result:\n" + "\n".join(_completed_outputs[-3:])
                if _completed_outputs
                else "No tasks completed before instability."
            )
            goal.error = f"Replan storm detected after {goal.replan_count} replans — goal frozen. {_partial}"
            goal.completed_at = now_iso
            record_intervention(
                goal.stability,
                "Replan storm: too many replans in short window",
            )
            goal.telemetry.stability_interventions += 1
            await self._emit(GoalEvent(
                event_name="goal.stability_intervention",
                goal_id=goal.id,
                chat_id=goal.chat_id,
                detail="Replan storm detected — goal frozen with partial result",
            ))
            _storm_msg = f"No pude completar la tarea después de {goal.replan_count} replaneaciones. "
            if _completed_outputs:
                _storm_msg += "Resultado parcial:\n" + "\n".join(_completed_outputs[-2:])
            else:
                _storm_msg += f"Objetivo: {goal.objective[:120]}"
            await self._notify_simple(goal, _storm_msg)
            logger.warning(
                "goal_orchestrator.replan_storm", goal_id=goal.id,
                replan_count=goal.replan_count,
            )
            return goal

        # ── Budget: replan count ───────────────────────────────────────
        try:
            check_replan(goal.budget)
        except BudgetError as e:
            goal.state = GoalState.FAILED
            goal.error = f"Replan budget exhausted ({e.used}/{e.limit})"
            goal.completed_at = now_iso
            mark_exceeded(goal.budget, "replans")
            goal.telemetry.budget_exceeded_events += 1
            await self._emit(GoalEvent(
                event_name="goal.budget_exceeded",
                goal_id=goal.id,
                chat_id=goal.chat_id,
                detail=goal.error,
            ))
            logger.error(
                "goal_orchestrator.replan_budget_exhausted",
                goal_id=goal.id,
                replans=e.used,
            )
            return goal

        # ── Hard cap (legacy MAX_REPLAN_COUNT) ─────────────────────────
        if goal.replan_count >= MAX_REPLAN_COUNT:
            goal.state = GoalState.FAILED
            goal.error = f"Replan limit ({MAX_REPLAN_COUNT}) exhausted"
            goal.completed_at = now_iso
            logger.error(
                "goal_orchestrator.replan_limit",
                goal_id=goal.id,
                replan_count=goal.replan_count,
            )
            return goal

        # ── Objective 2 Safeguard 1: Check plan lock before replanning ─────
        if goal.plan_locked:
            # Plan is locked — only replan if a task actually permanently failed.
            # If somehow triggered in another context, skip the replan silently.
            failed_perm = [
                n for n in (goal.task_graph.nodes if goal.task_graph else [])
                if n.status == TaskStatus.FAILED and n.retries >= n.max_retries
            ]
            if not failed_perm:
                logger.info(
                    "goal_orchestrator.plan_lock_blocked_replan",
                    goal_id=goal.id,
                    reason="plan_locked but no permanently failed tasks",
                )
                # Don't replan; keep goal active, it may recover
                return goal

        goal.replan_count += 1
        record_replan(goal.budget)

        logger.info(
            "goal_orchestrator.plan_replan",
            plan_type="plan_replan",
            goal_id=goal.id,
            attempt=goal.replan_count,
            plan_locked=goal.plan_locked,
        )
        await self._emit(GoalEvent(
            event_name="goal.replanned",
            goal_id=goal.id,
            chat_id=goal.chat_id,
            detail=f"Replanning attempt {goal.replan_count}/{MAX_REPLAN_COUNT}",
        ))

        # Build completed-task context for the planner
        completed_summaries = [
            f"- {n.skill_name}({n.arguments}): DONE"
            for n in goal.task_graph.nodes
            if n.status == TaskStatus.DONE
        ]
        replan_constraints = goal.constraints
        if completed_summaries:
            replan_constraints = (
                (replan_constraints + "\n" if replan_constraints else "")
                + "Already completed tasks (DO NOT repeat these):\n"
                + "\n".join(completed_summaries)
            )
        failed_tasks = [
            n for n in goal.task_graph.nodes if n.status == TaskStatus.FAILED
        ]
        if failed_tasks:
            failed_desc = "\n".join(
                f"- {n.skill_name}: {n.error[:100]}" for n in failed_tasks
            )
            replan_constraints += f"\n\nFailed tasks to avoid/replace:\n{failed_desc}"

        replan_goal = Goal(
            id=goal.id,
            objective=goal.objective,
            constraints=replan_constraints,
            success_criteria=goal.success_criteria,
            chat_id=goal.chat_id,
            user_id=goal.user_id,
        )

        new_graph, error, tokens_used = await self.plan_generator.generate(replan_goal)
        record_planning_tokens(goal.budget, tokens_used)

        if new_graph is None:
            goal.state = GoalState.FAILED
            goal.error = f"Replanning failed: {error}"
            goal.completed_at = now_iso
            logger.error(
                "goal_orchestrator.replan_failed", goal_id=goal.id, error=error
            )
            return goal

        # Merge: keep DONE nodes; replace rest with new nodes
        done_nodes = [n for n in goal.task_graph.nodes if n.status == TaskStatus.DONE]
        done_ids = {n.id for n in done_nodes}

        id_map: dict[str, str] = {}
        for node in new_graph.nodes:
            new_id = node.id
            if new_id in done_ids:
                new_id = f"r{goal.replan_count}_{node.id}"
            id_map[node.id] = new_id
            node.id = new_id
        for node in new_graph.nodes:
            node.dependencies = [id_map.get(dep, dep) for dep in node.dependencies]

        merged_nodes = done_nodes + new_graph.nodes
        goal.task_graph.nodes = merged_nodes
        valid, dag_error = goal.task_graph.validate_dag()
        if not valid:
            goal.task_graph = new_graph
            logger.warning(
                "goal_orchestrator.replan_dag_invalid",
                goal_id=goal.id,
                error=dag_error,
            )

        goal.progress = goal.task_graph.compute_progress()
        logger.info(
            "goal_orchestrator.replanned",
            goal_id=goal.id,
            total_tasks=goal.task_graph.total_tasks,
            progress=goal.progress,
            tokens_used=tokens_used,
        )
        return goal

    # ------------------------------------------------------------------
    # Pause / Resume / Cancel
    async def invoke(self, goal_id: str) -> str:
        """Advance a specific goal by one tick step.

        Called by AgentRuntime to progress agent-owned goals without waiting
        for the next global tick cycle.
        Returns a status string (e.g. "skip:planning", "task_done", "completed").
        """
        from ..policy import with_trace
        async with with_trace(
            self.redis_url, path="goal_executor",
            chat_id=goal_id[:12], user_text=f"goal_invoke:{goal_id[:8]}",
        ) as _trace:
            r = await self._redis()
            try:
                goal = await load_goal(r, goal_id)
            finally:
                await r.aclose()

            if goal is None:
                _trace.add_guard("goal:not_found")
                return f"not_found:{goal_id[:8]}"
            _trace.chat_id = str(goal.chat_id or goal_id[:12])
            if goal.state == GoalState.ACTIVE:
                result = await self._tick_one(goal)
                _trace.add_guard(f"goal:tick[{result}]")
                return result
            _trace.add_guard(f"goal:skip[{goal.state.value}]")
            return f"skip:{goal.state.value}"

    # ------------------------------------------------------------------

    async def pause_goal(self, goal_id: str) -> tuple[bool, str]:
        r = await self._redis()
        try:
            goal = await load_goal(r, goal_id)
            if goal is None:
                return False, "Goal not found"
            if goal.state != GoalState.ACTIVE:
                return False, f"Goal is {goal.state.value}, not active"
            goal.state = GoalState.PAUSED
            await save_goal(r, goal)
            await self._emit(GoalEvent(
                event_name="goal.paused",
                goal_id=goal_id,
                chat_id=goal.chat_id,
                detail="Paused by user",
            ))
            return True, "Goal paused"
        finally:
            await r.aclose()

    async def resume_goal(self, goal_id: str) -> tuple[bool, str]:
        r = await self._redis()
        try:
            goal = await load_goal(r, goal_id)
            if goal is None:
                return False, "Goal not found"
            if goal.state != GoalState.PAUSED:
                return False, f"Goal is {goal.state.value}, not paused"

            # Unblock BLOCKED tasks and clear budget_exceeded flag for manual override
            for node in goal.task_graph.nodes:
                if node.status == TaskStatus.BLOCKED:
                    node.status = TaskStatus.PENDING
                    node.error = ""

            # Clear budget exceeded flag (user explicitly resumed)
            if goal.budget.budget_exceeded:
                goal.budget.budget_exceeded = False
                goal.budget.budget_exceeded_dimension = ""

            # Clear stability lock if user resumes (explicit override)
            if goal.stability.locked:
                goal.stability.locked = False
                record_intervention(
                    goal.stability,
                    "Lock cleared: resumed by user",
                )

            goal.state = GoalState.ACTIVE
            await save_goal(r, goal)
            await self._emit(GoalEvent(
                event_name="goal.resumed",
                goal_id=goal_id,
                chat_id=goal.chat_id,
                detail="Resumed by user",
            ))
            return True, "Goal resumed"
        finally:
            await r.aclose()

    async def cancel_goal(self, goal_id: str) -> tuple[bool, str]:
        r = await self._redis()
        try:
            goal = await load_goal(r, goal_id)
            if goal is None:
                return False, "Goal not found"
            if goal.state in (GoalState.COMPLETED, GoalState.FAILED):
                return False, f"Goal already {goal.state.value}"
            goal.state = GoalState.FAILED
            goal.error = "Cancelled by user"
            goal.completed_at = datetime.now(timezone.utc).isoformat()
            await save_goal(r, goal)
            # Event sourcing — GoalCancelled
            asyncio.get_running_loop().create_task(emit_goal_event(
                self.redis_url, "GoalCancelled", goal_id, state="failed",
            ))
            await self._emit(GoalEvent(
                event_name="goal.failed",
                goal_id=goal_id,
                chat_id=goal.chat_id,
                detail="Cancelled by user",
            ))
            return True, "Goal cancelled"
        finally:
            await r.aclose()

    async def set_autonomy_mode(
        self, goal_id: str, mode: AutonomyMode
    ) -> tuple[bool, str]:
        """Switch autonomy mode on an active or paused goal."""
        r = await self._redis()
        try:
            goal = await load_goal(r, goal_id)
            if goal is None:
                return False, "Goal not found"
            if goal.state in (GoalState.COMPLETED, GoalState.FAILED):
                return False, f"Cannot change autonomy on {goal.state.value} goal"
            old_mode = goal.autonomy_mode.value
            goal.autonomy_mode = mode
            await save_goal(r, goal)
            await self._emit(GoalEvent(
                event_name="goal.autonomy_changed",
                goal_id=goal_id,
                chat_id=goal.chat_id,
                detail=f"{old_mode} → {mode.value}",
            ))
            return True, f"Autonomy mode changed: {old_mode} → {mode.value}"
        finally:
            await r.aclose()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _redis(self):
        return aioredis.from_url(self.redis_url, decode_responses=True)

    async def _emit(self, event: GoalEvent) -> None:
        try:
            logger.info(
                f"goal_event.{event.event_name}",
                goal_id=event.goal_id,
                task_id=event.task_id,
                detail=(event.detail[:100] if event.detail else ""),
            )
            from ..db.models import AuditLog
            from ..db.session import async_session
            async with async_session() as session:
                audit = AuditLog(
                    id=str(uuid4()),
                    event_type=f"goal.{event.event_name.split('.')[-1]}",
                    source="goal_orchestrator",
                    action=event.event_name,
                    input_summary=event.goal_id,
                    output_summary=(event.detail[:200] if event.detail else ""),
                    user_id="",
                    chat_id=event.chat_id,
                    latency_ms=event.latency_ms,
                )
                session.add(audit)
                await session.commit()
        except Exception:
            logger.exception("goal_orchestrator.emit_error")

    async def _notify_simple(self, goal: Goal, text: str) -> None:
        chat_id = goal.chat_id or self.default_chat_id
        if not chat_id:
            return
        try:
            import re as _re
            text = _re.sub(r"\[TAREA PROGRAMADA:[^\]]*\]\s*\n?EJECUTA AHORA[^\n]*\n?", "", text).strip()
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
            pass


# ---------------------------------------------------------------------------
# CPU backpressure helper
# ---------------------------------------------------------------------------


def _cpu_overloaded(threshold: float = 85.0) -> bool:
    """Return True if CPU usage exceeds threshold (non-blocking check)."""
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=None)
        return cpu >= threshold
    except Exception:
        return False
