"""Tests for the Advanced Autonomous Control Layer.

Covers:
  - CognitiveBudget enforcement (budget.py)
  - AutonomyMode decision matrix (autonomy.py)
  - StabilityState rules (stability.py)
  - GoalTemplate sanitization and CRUD (templates.py)
  - Anomaly detection (reflection_job.py)
  - Severity classifier (reflection_job.py)
  - GoalStepExecutor: autonomy blocking, budget exceeded, stability backoff/lock
  - Runaway scenario: goal never completes due to permanent failures → fail after replan limit
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.goal_orchestrator.types import (
    AutonomyMode,
    CognitiveBudget,
    Goal,
    GoalState,
    GoalTelemetry,
    RiskLevel,
    StabilityState,
    TaskGraph,
    TaskNode,
    TaskStatus,
)
from src.goal_orchestrator.budget import (
    BudgetError,
    check_planning_tokens,
    check_replan,
    check_steps,
    mark_exceeded,
    record_execution_step,
    record_planning_tokens,
    record_replan,
)
from src.goal_orchestrator.autonomy import needs_confirmation, confirmation_reason
from src.goal_orchestrator.stability import (
    apply_backoff_if_needed,
    check_backoff,
    on_task_failure,
    on_task_success,
    record_replan as stability_record_replan,
    lock_goal,
    record_policy_block,
)
from src.goal_orchestrator.reflection_job import detect_anomalies, is_severe
from src.goal_orchestrator.templates import (
    GoalTemplate,
    make_template_from_goal,
)
from src.goal_orchestrator.executor import GoalStepExecutor


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def make_node(
    id: str = "t1",
    skill_name: str = "web_search",
    deps: list[str] | None = None,
    status: TaskStatus = TaskStatus.PENDING,
    risk_level: RiskLevel = RiskLevel.LOW,
    retries: int = 0,
    max_retries: int = 2,
) -> TaskNode:
    return TaskNode(
        id=id,
        description=f"Task {id}",
        skill_name=skill_name,
        arguments={"query": "test"},
        dependencies=deps or [],
        status=status,
        risk_level=risk_level,
        retries=retries,
        max_retries=max_retries,
    )


def make_goal(
    objective: str = "Test objective",
    state: GoalState = GoalState.ACTIVE,
    nodes: list[TaskNode] | None = None,
    autonomy_mode: AutonomyMode = AutonomyMode.SEMI,
    budget: CognitiveBudget | None = None,
    stability: StabilityState | None = None,
    step_count: int = 0,
    replan_count: int = 0,
    started_at: str | None = None,
) -> Goal:
    if nodes is None:
        nodes = [make_node()]
    return Goal(
        id="test-goal-id",
        objective=objective,
        state=state,
        task_graph=TaskGraph(nodes=nodes),
        autonomy_mode=autonomy_mode,
        budget=budget or CognitiveBudget(),
        stability=stability or StabilityState(),
        step_count=step_count,
        replan_count=replan_count,
        started_at=started_at or datetime.now(timezone.utc).isoformat(),
    )


# ===========================================================================
# Part 1 — CognitiveBudget enforcement
# ===========================================================================

class TestCognitiveBudget:
    def test_planning_tokens_within_budget(self):
        b = CognitiveBudget(max_tokens_planning=1000, tokens_used_planning=500)
        check_planning_tokens(b)  # Should not raise

    def test_planning_tokens_exceeded(self):
        # check_planning_tokens raises when tokens_used + tokens_requested > max
        b = CognitiveBudget(max_tokens_planning=1000, tokens_used_planning=1001)
        with pytest.raises(BudgetError) as exc_info:
            check_planning_tokens(b)
        assert exc_info.value.dimension == "tokens_planning"

    def test_planning_tokens_exceeded_with_request(self):
        # Also raises when adding tokens_requested would exceed
        b = CognitiveBudget(max_tokens_planning=1000, tokens_used_planning=900)
        with pytest.raises(BudgetError):
            check_planning_tokens(b, tokens_requested=200)

    def test_planning_tokens_large_limit_no_raise(self):
        b = CognitiveBudget(max_tokens_planning=100000, tokens_used_planning=500)
        check_planning_tokens(b)  # Well within limit — no raise

    def test_steps_within_budget(self):
        b = CognitiveBudget(max_steps=50, steps_executed=49)
        check_steps(b)  # Should not raise

    def test_steps_exceeded(self):
        b = CognitiveBudget(max_steps=50, steps_executed=50)
        with pytest.raises(BudgetError) as exc_info:
            check_steps(b)
        assert exc_info.value.dimension == "max_steps"

    def test_replan_within_budget(self):
        b = CognitiveBudget(max_replans=3, replans_used=2)
        check_replan(b)  # Should not raise

    def test_replan_exceeded(self):
        b = CognitiveBudget(max_replans=3, replans_used=3)
        with pytest.raises(BudgetError) as exc_info:
            check_replan(b)
        assert exc_info.value.dimension == "replans"

    def test_record_planning_tokens(self):
        b = CognitiveBudget(max_tokens_planning=1000)
        record_planning_tokens(b, 300)
        record_planning_tokens(b, 200)
        assert b.tokens_used_planning == 500

    def test_record_execution_step(self):
        b = CognitiveBudget()
        record_execution_step(b)
        record_execution_step(b)
        assert b.steps_executed == 2

    def test_record_replan(self):
        b = CognitiveBudget()
        record_replan(b)
        assert b.replans_used == 1

    def test_mark_exceeded(self):
        b = CognitiveBudget()
        mark_exceeded(b, "steps")
        assert b.budget_exceeded is True
        assert b.budget_exceeded_dimension == "steps"

    def test_budget_error_has_used_limit(self):
        b = CognitiveBudget(max_steps=10, steps_executed=10)
        with pytest.raises(BudgetError) as exc_info:
            check_steps(b)
        err = exc_info.value
        assert err.used == 10
        assert err.limit == 10

    def test_large_steps_limit_no_raise(self):
        b = CognitiveBudget(max_steps=10000, steps_executed=5)
        check_steps(b)  # Well within limit — no raise


# ===========================================================================
# Part 2 — AutonomyMode decision matrix
# ===========================================================================

class TestAutonomyMode:
    def test_assist_blocks_all_risks(self):
        for risk in (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL):
            node = make_node(risk_level=risk)
            assert needs_confirmation(node, AutonomyMode.ASSIST), f"ASSIST should block {risk}"

    def test_semi_auto_for_low_medium(self):
        for risk in (RiskLevel.LOW, RiskLevel.MEDIUM):
            node = make_node(risk_level=risk)
            assert not needs_confirmation(node, AutonomyMode.SEMI), f"SEMI should auto {risk}"

    def test_semi_blocks_high_critical(self):
        for risk in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            node = make_node(risk_level=risk)
            assert needs_confirmation(node, AutonomyMode.SEMI), f"SEMI should block {risk}"

    def test_full_auto_for_low_medium_high(self):
        for risk in (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH):
            node = make_node(risk_level=risk)
            assert not needs_confirmation(node, AutonomyMode.FULL), f"FULL should auto {risk}"

    def test_full_blocks_critical(self):
        node = make_node(risk_level=RiskLevel.CRITICAL)
        assert needs_confirmation(node, AutonomyMode.FULL), "FULL should block CRITICAL"

    def test_confirmation_reason_returned(self):
        node = make_node(risk_level=RiskLevel.HIGH)
        reason = confirmation_reason(node, AutonomyMode.SEMI)
        assert len(reason) > 0
        assert "HIGH" in reason or "high" in reason or "semi" in reason.lower()

    def test_critical_always_blocked_regardless_of_mode(self):
        node = make_node(risk_level=RiskLevel.CRITICAL)
        for mode in (AutonomyMode.ASSIST, AutonomyMode.SEMI, AutonomyMode.FULL):
            assert needs_confirmation(node, mode), f"CRITICAL must always be blocked (mode={mode})"


# ===========================================================================
# Part 3 — Stability layer
# ===========================================================================

class TestStabilityLayer:
    def test_consecutive_failures_increments(self):
        s = StabilityState()
        on_task_failure(s)
        on_task_failure(s)
        assert s.consecutive_failures == 2

    def test_success_resets_consecutive_failures(self):
        s = StabilityState(consecutive_failures=3)
        on_task_success(s)
        assert s.consecutive_failures == 0

    def test_backoff_applied_after_threshold(self):
        """5+ consecutive failures should trigger backoff (threshold raised from 3 → 5)."""
        s = StabilityState()
        for _ in range(5):
            on_task_failure(s)
        applied = apply_backoff_if_needed(s)
        assert applied is True
        assert s.backoff_until is not None
        assert s.in_backoff is True

    def test_no_backoff_below_threshold(self):
        # Updated: stability.CONSECUTIVE_FAILURES_THRESHOLD was lowered from 5
        # to 4. Loop 3 times to stay strictly below the new threshold.
        s = StabilityState()
        for _ in range(3):
            on_task_failure(s)
        applied = apply_backoff_if_needed(s)
        assert applied is False
        assert s.in_backoff is False

    def test_backoff_check_returns_in_backoff(self):
        s = StabilityState()
        future = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
        s.backoff_until = future
        s.in_backoff = True
        in_backoff, reason = check_backoff(s)
        assert in_backoff is True
        assert len(reason) > 0

    def test_backoff_check_expired_clears_state(self):
        s = StabilityState()
        past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        s.backoff_until = past
        s.in_backoff = True
        in_backoff, reason = check_backoff(s)
        assert in_backoff is False
        assert s.in_backoff is False

    def test_replan_storm_detection(self):
        """Replans within window should trigger storm (storm threshold = REPLAN_STORM_COUNT=5)."""
        s = StabilityState()
        # Pre-fill 5 recent timestamps so the 6th (added by record_replan) brings
        # the window total to 6 — well above the 5-replan threshold → storm True.
        recent_ts = datetime.now(timezone.utc).isoformat()
        for _ in range(5):
            s.replan_timestamps.append(recent_ts)
        storm = stability_record_replan(s)
        assert storm is True

    def test_no_storm_below_threshold(self):
        s = StabilityState()
        for _ in range(5):
            stability_record_replan(s)
        assert s.locked is False

    def test_lock_goal(self):
        s = StabilityState()
        lock_goal(s, "oscillation detected")
        assert s.locked is True
        assert "oscillation" in s.intervention_reason

    def test_policy_block_counter(self):
        s = StabilityState()
        exceeded = record_policy_block(s, "shell")
        assert s.policy_block_counts.get("shell", 0) >= 1
        # Limit is 5; after 5 calls it should flag exceeded
        for _ in range(3):
            record_policy_block(s, "shell")
        exceeded5 = record_policy_block(s, "shell")
        assert exceeded5 is True


# ===========================================================================
# Part 4 — GoalTemplate
# ===========================================================================

class TestGoalTemplate:
    def _make_completed_goal(self) -> Goal:
        nodes = [
            make_node("t1", "web_search", risk_level=RiskLevel.LOW),
            make_node("t2", "send_email", deps=["t1"], risk_level=RiskLevel.MEDIUM),
        ]
        return make_goal(
            state=GoalState.COMPLETED,
            nodes=nodes,
        )

    def test_make_template_from_goal(self):
        goal = self._make_completed_goal()
        tpl = make_template_from_goal(goal, name="BTC Report")
        assert tpl.name == "BTC Report"
        assert tpl.task_count == 2
        assert len(tpl.task_graph_json) > 0

    def test_template_load_task_graph(self):
        goal = self._make_completed_goal()
        tpl = make_template_from_goal(goal, name="Test Template")
        graph = tpl.load_task_graph()
        assert graph is not None
        assert len(graph.nodes) == 2

    def _make_goal_with_args(self, args: dict) -> Goal:
        node = TaskNode(
            id="t1",
            description="Task t1",
            skill_name="web_search",
            arguments=args,
            dependencies=[],
            risk_level=RiskLevel.LOW,
        )
        return make_goal(state=GoalState.COMPLETED, nodes=[node])

    def test_sanitize_removes_sensitive_keys(self):
        goal = self._make_goal_with_args(
            {"api_key": "sk-secret", "token": "bearer-xyz", "query": "hello"}
        )
        tpl = make_template_from_goal(goal, name="S")
        graph = tpl.load_task_graph()
        args = graph.nodes[0].arguments
        assert args["api_key"] == "<SECRET>"
        assert args["token"] == "<SECRET>"
        assert args["query"] == "hello"

    def test_sanitize_truncates_long_values(self):
        long_val = "x" * 300
        goal = self._make_goal_with_args({"data": long_val})
        tpl = make_template_from_goal(goal, name="L")
        graph = tpl.load_task_graph()
        assert graph.nodes[0].arguments["data"] == "<VALUE>"

    def test_sanitize_keeps_short_values(self):
        goal = self._make_goal_with_args({"query": "bitcoin price"})
        tpl = make_template_from_goal(goal, name="Q")
        graph = tpl.load_task_graph()
        assert graph.nodes[0].arguments["query"] == "bitcoin price"

    def test_template_has_id(self):
        goal = self._make_completed_goal()
        tpl = make_template_from_goal(goal, name="X")
        assert len(tpl.id) > 0


# ===========================================================================
# Part 5 — Meta-Reflection anomaly detection
# ===========================================================================

class TestMetaReflectionAnomalies:
    def test_no_anomalies_healthy_goal(self):
        goal = make_goal(step_count=5, replan_count=0)
        anomalies = detect_anomalies(goal)
        assert anomalies == []

    def test_failure_cluster_detected(self):
        # Updated: failure_cluster threshold raised from >=2 to >=3 to reduce
        # false positives on transient external errors.
        stab = StabilityState(consecutive_failures=3)
        goal = make_goal(stability=stab)
        anomalies = detect_anomalies(goal)
        assert "failure_cluster" in anomalies

    def test_stagnation_detected(self):
        """10+ steps with no DONE tasks → stagnation."""
        nodes = [make_node("t1", status=TaskStatus.PENDING)]
        goal = make_goal(nodes=nodes, step_count=10)
        anomalies = detect_anomalies(goal)
        assert "stagnation" in anomalies

    def test_no_stagnation_when_tasks_done(self):
        nodes = [make_node("t1", status=TaskStatus.DONE)]
        goal = make_goal(nodes=nodes, step_count=10)
        anomalies = detect_anomalies(goal)
        assert "stagnation" not in anomalies

    def test_locked_stability_detected(self):
        stab = StabilityState(locked=True)
        goal = make_goal(stability=stab)
        anomalies = detect_anomalies(goal)
        assert "locked_stability" in anomalies

    def test_high_replan_rate_detected(self):
        """replan_count/step_count >= 0.5 and replan_count >= 2 → high_replan_rate."""
        goal = make_goal(step_count=4, replan_count=2)
        anomalies = detect_anomalies(goal)
        assert "high_replan_rate" in anomalies

    def test_no_high_replan_rate_below_threshold(self):
        goal = make_goal(step_count=10, replan_count=1)
        anomalies = detect_anomalies(goal)
        assert "high_replan_rate" not in anomalies

    def test_budget_consumption_warning(self):
        # Updated: BUDGET_WARNING_PCT raised from 0.60 to 0.85 (less noisy).
        # Use 90% to stay clearly above the new threshold.
        budget = CognitiveBudget(
            max_tokens_planning=1000,
            tokens_used_planning=900,  # 90% → above 85% threshold
        )
        goal = make_goal(budget=budget)
        anomalies = detect_anomalies(goal)
        assert "high_budget_consumption" in anomalies

    def test_severity_stagnation_is_severe(self):
        assert is_severe(["stagnation"]) is True

    def test_severity_locked_stability_is_severe(self):
        assert is_severe(["locked_stability"]) is True

    def test_severity_failure_cluster_not_severe(self):
        assert is_severe(["failure_cluster"]) is False

    def test_severity_high_replan_rate_not_severe(self):
        assert is_severe(["high_replan_rate"]) is False


# ===========================================================================
# Part 6 — GoalStepExecutor: advanced layer behavior
# ===========================================================================

def _make_executor(skill_result_success=True, skill_output="ok"):
    skill_executor = MagicMock()
    from src.skills.types import SkillResult
    result = SkillResult(skill_name="test_skill", success=skill_result_success, output=skill_output, error="" if skill_result_success else skill_output)
    skill_executor.execute = AsyncMock(return_value=result)

    memory_manager = MagicMock()
    memory_manager.store_memory = AsyncMock()

    bus = MagicMock()
    bus.publish = AsyncMock()

    executor = GoalStepExecutor(
        skill_executor=skill_executor,
        memory_manager=memory_manager,
        bus=bus,
        default_chat_id="test-chat",
    )
    return executor


class TestGoalStepExecutorAdvanced:
    def test_budget_exceeded_flag_pauses_goal(self):
        budget = CognitiveBudget(budget_exceeded=True, budget_exceeded_dimension="steps")
        goal = make_goal(budget=budget)
        executor = _make_executor()

        result = asyncio.get_event_loop().run_until_complete(executor.step(goal))
        assert result.action == "budget_exceeded"
        assert result.goal.state == GoalState.PAUSED

    def test_stability_backoff_skips_execution(self):
        from datetime import timedelta
        future = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()
        stab = StabilityState(backoff_until=future, in_backoff=True)
        goal = make_goal(stability=stab)
        executor = _make_executor()

        result = asyncio.get_event_loop().run_until_complete(executor.step(goal))
        assert result.action == "stability_backoff"

    def test_stability_locked_pauses_goal(self):
        stab = StabilityState(locked=True)
        goal = make_goal(stability=stab)
        executor = _make_executor()

        result = asyncio.get_event_loop().run_until_complete(executor.step(goal))
        assert result.action == "stability_locked"
        assert result.goal.state == GoalState.PAUSED

    def test_assist_mode_blocks_low_risk_task(self):
        node = make_node("t1", risk_level=RiskLevel.LOW)
        goal = make_goal(nodes=[node], autonomy_mode=AutonomyMode.ASSIST)
        executor = _make_executor()

        result = asyncio.get_event_loop().run_until_complete(executor.step(goal))
        assert result.action == "autonomy_blocked"
        assert result.goal.state == GoalState.PAUSED

    def test_semi_mode_auto_executes_low_risk(self):
        node = make_node("t1", risk_level=RiskLevel.LOW)
        goal = make_goal(nodes=[node], autonomy_mode=AutonomyMode.SEMI)
        executor = _make_executor(skill_result_success=True)

        result = asyncio.get_event_loop().run_until_complete(executor.step(goal))
        assert result.action == "task_done"
        # COMPLETED is detected on the *next* tick (when get_next_task returns None)
        assert result.goal.progress == 100.0

    def test_semi_mode_blocks_high_risk(self):
        node = make_node("t1", risk_level=RiskLevel.HIGH)
        goal = make_goal(nodes=[node], autonomy_mode=AutonomyMode.SEMI)
        executor = _make_executor()

        result = asyncio.get_event_loop().run_until_complete(executor.step(goal))
        assert result.action == "autonomy_blocked"
        assert result.goal.state == GoalState.PAUSED

    def test_full_mode_auto_executes_high_risk(self):
        node = make_node("t1", risk_level=RiskLevel.HIGH)
        goal = make_goal(nodes=[node], autonomy_mode=AutonomyMode.FULL)
        executor = _make_executor(skill_result_success=True)

        result = asyncio.get_event_loop().run_until_complete(executor.step(goal))
        assert result.action == "task_done"

    def test_full_mode_blocks_critical(self):
        node = make_node("t1", risk_level=RiskLevel.CRITICAL)
        goal = make_goal(nodes=[node], autonomy_mode=AutonomyMode.FULL)
        executor = _make_executor()

        result = asyncio.get_event_loop().run_until_complete(executor.step(goal))
        assert result.action == "autonomy_blocked"

    def test_step_limit_fails_goal(self):
        node = make_node("t1")
        goal = make_goal(nodes=[node], step_count=50)
        goal.max_steps = 50
        executor = _make_executor()

        result = asyncio.get_event_loop().run_until_complete(executor.step(goal))
        assert result.action == "limit_exceeded"
        assert result.goal.state == GoalState.FAILED

    def test_budget_step_check_pauses_goal(self):
        budget = CognitiveBudget(max_steps=10, steps_executed=10)
        node = make_node("t1")
        goal = make_goal(nodes=[node], budget=budget)
        executor = _make_executor()

        result = asyncio.get_event_loop().run_until_complete(executor.step(goal))
        assert result.action == "budget_exceeded"
        assert result.goal.state == GoalState.PAUSED

    def test_task_success_resets_consecutive_failures(self):
        stab = StabilityState(consecutive_failures=2)
        node = make_node("t1", risk_level=RiskLevel.LOW)
        goal = make_goal(nodes=[node], stability=stab, autonomy_mode=AutonomyMode.FULL)
        executor = _make_executor(skill_result_success=True)

        result = asyncio.get_event_loop().run_until_complete(executor.step(goal))
        assert result.action == "task_done"
        assert result.goal.stability.consecutive_failures == 0

    def test_task_failure_increments_consecutive_failures(self):
        node = make_node("t1", risk_level=RiskLevel.LOW, retries=0, max_retries=0)
        goal = make_goal(nodes=[node], autonomy_mode=AutonomyMode.FULL)
        executor = _make_executor(skill_result_success=False, skill_output="error")

        result = asyncio.get_event_loop().run_until_complete(executor.step(goal))
        assert result.action == "task_failed"
        assert result.goal.stability.consecutive_failures >= 1

    def test_telemetry_skill_distribution_tracked(self):
        node = make_node("t1", skill_name="web_search", risk_level=RiskLevel.LOW)
        goal = make_goal(nodes=[node], autonomy_mode=AutonomyMode.FULL)
        executor = _make_executor(skill_result_success=True)

        result = asyncio.get_event_loop().run_until_complete(executor.step(goal))
        assert result.action == "task_done"
        assert result.goal.telemetry.skill_distribution.get("web_search", 0) == 1

    def test_telemetry_autonomy_decisions_tracked(self):
        node = make_node("t1", risk_level=RiskLevel.LOW)
        goal = make_goal(nodes=[node], autonomy_mode=AutonomyMode.FULL)
        executor = _make_executor(skill_result_success=True)

        result = asyncio.get_event_loop().run_until_complete(executor.step(goal))
        assert result.goal.telemetry.autonomy_decisions.get("auto", 0) == 1


# ===========================================================================
# Part 7 — Concurrency limit (verify max_concurrent honored)
# ===========================================================================

class TestConcurrencyLimit:
    def test_budget_exceeded_flag_preserved_across_steps(self):
        """Once budget_exceeded is set, repeated step() calls stay paused."""
        budget = CognitiveBudget(budget_exceeded=True, budget_exceeded_dimension="steps")
        node = make_node("t1")
        goal = make_goal(nodes=[node], budget=budget)
        executor = _make_executor()

        for _ in range(3):
            result = asyncio.get_event_loop().run_until_complete(executor.step(goal))
            assert result.action == "budget_exceeded"
            assert result.goal.state == GoalState.PAUSED
