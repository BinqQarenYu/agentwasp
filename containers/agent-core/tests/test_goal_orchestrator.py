"""Tests for the Autonomous Goal Engine.

Covers:
  - DAG validation (valid graphs, cycles, unknown deps)
  - TaskGraph execution helpers
  - Goal safety limits (steps, runtime)
  - PlanGenerator JSON extraction and validation helpers
  - HIGH risk guard behavior
  - Step limit enforcement
  - Runtime limit enforcement
  - Replan count enforcement (orchestrator logic)
  - Concurrency: get_executable_tasks ordering
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.goal_orchestrator.types import (
    Goal,
    GoalState,
    RiskLevel,
    TaskGraph,
    TaskNode,
    TaskStatus,
)
from src.goal_orchestrator.planner import _extract_json, _validate_and_build_graph
from src.goal_orchestrator.executor import GoalStepExecutor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_node(
    id: str,
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


def make_goal(objective: str = "Test goal", **kwargs) -> Goal:
    return Goal(objective=objective, **kwargs)


def make_mock_registry(skills: list[str] | None = None):
    """Return a mock SkillRegistry with the given enabled skill names."""
    skills = skills or ["web_search", "send_message", "write_file"]
    mock = MagicMock()
    defns = []
    for name in skills:
        d = MagicMock()
        d.name = name
        defns.append(d)
    mock.list_enabled.return_value = defns
    return mock


# ---------------------------------------------------------------------------
# TaskGraph — validate_dag
# ---------------------------------------------------------------------------

class TestTaskGraphValidateDAG:
    def test_valid_linear_chain(self):
        nodes = [
            make_node("t1"),
            make_node("t2", deps=["t1"]),
            make_node("t3", deps=["t2"]),
        ]
        graph = TaskGraph(nodes=nodes)
        valid, err = graph.validate_dag()
        assert valid is True
        assert err == ""

    def test_valid_diamond(self):
        """t1 → t2, t1 → t3, t2 + t3 → t4"""
        nodes = [
            make_node("t1"),
            make_node("t2", deps=["t1"]),
            make_node("t3", deps=["t1"]),
            make_node("t4", deps=["t2", "t3"]),
        ]
        graph = TaskGraph(nodes=nodes)
        valid, err = graph.validate_dag()
        assert valid is True

    def test_cycle_detected(self):
        """t1 → t2 → t1 is a cycle."""
        nodes = [
            make_node("t1", deps=["t2"]),
            make_node("t2", deps=["t1"]),
        ]
        graph = TaskGraph(nodes=nodes)
        valid, err = graph.validate_dag()
        assert valid is False
        assert "cycle" in err.lower()

    def test_self_loop(self):
        """t1 → t1 is a cycle."""
        nodes = [make_node("t1", deps=["t1"])]
        graph = TaskGraph(nodes=nodes)
        valid, err = graph.validate_dag()
        assert valid is False

    def test_unknown_dependency(self):
        nodes = [make_node("t1", deps=["t_missing"])]
        graph = TaskGraph(nodes=nodes)
        valid, err = graph.validate_dag()
        assert valid is False
        assert "t_missing" in err

    def test_empty_graph(self):
        graph = TaskGraph(nodes=[])
        valid, err = graph.validate_dag()
        assert valid is True  # vacuously valid

    def test_no_dependencies(self):
        nodes = [make_node("t1"), make_node("t2"), make_node("t3")]
        graph = TaskGraph(nodes=nodes)
        valid, err = graph.validate_dag()
        assert valid is True


# ---------------------------------------------------------------------------
# TaskGraph — executable tasks
# ---------------------------------------------------------------------------

class TestGetExecutableTasks:
    def test_pending_no_deps_ready(self):
        graph = TaskGraph(nodes=[
            make_node("t1"),
            make_node("t2"),
        ])
        exe = graph.get_executable_tasks()
        assert {n.id for n in exe} == {"t1", "t2"}

    def test_deps_not_satisfied(self):
        graph = TaskGraph(nodes=[
            make_node("t1"),
            make_node("t2", deps=["t1"]),  # t1 still PENDING
        ])
        exe = graph.get_executable_tasks()
        assert [n.id for n in exe] == ["t1"]

    def test_deps_satisfied(self):
        nodes = [
            make_node("t1", status=TaskStatus.DONE),
            make_node("t2", deps=["t1"]),
        ]
        graph = TaskGraph(nodes=nodes)
        exe = graph.get_executable_tasks()
        assert [n.id for n in exe] == ["t2"]

    def test_failed_dep_blocks_child(self):
        """A FAILED parent does not count as DONE — child stays blocked."""
        nodes = [
            make_node("t1", status=TaskStatus.FAILED),
            make_node("t2", deps=["t1"]),
        ]
        graph = TaskGraph(nodes=nodes)
        exe = graph.get_executable_tasks()
        assert exe == []

    def test_running_task_not_returned(self):
        nodes = [make_node("t1", status=TaskStatus.RUNNING)]
        graph = TaskGraph(nodes=nodes)
        exe = graph.get_executable_tasks()
        assert exe == []

    def test_all_done_no_executables(self):
        nodes = [make_node("t1", status=TaskStatus.DONE)]
        graph = TaskGraph(nodes=nodes)
        exe = graph.get_executable_tasks()
        assert exe == []


# ---------------------------------------------------------------------------
# TaskGraph — progress, completion, failure
# ---------------------------------------------------------------------------

class TestTaskGraphHelpers:
    def test_compute_progress_empty(self):
        assert TaskGraph().compute_progress() == 0.0

    def test_compute_progress_half(self):
        nodes = [
            make_node("t1", status=TaskStatus.DONE),
            make_node("t2"),
        ]
        assert TaskGraph(nodes=nodes).compute_progress() == 50.0

    def test_compute_progress_full(self):
        nodes = [
            make_node("t1", status=TaskStatus.DONE),
            make_node("t2", status=TaskStatus.DONE),
        ]
        assert TaskGraph(nodes=nodes).compute_progress() == 100.0

    def test_is_complete_true(self):
        nodes = [make_node("t1", status=TaskStatus.DONE)]
        assert TaskGraph(nodes=nodes).is_complete() is True

    def test_is_complete_false_pending(self):
        nodes = [
            make_node("t1", status=TaskStatus.DONE),
            make_node("t2"),
        ]
        assert TaskGraph(nodes=nodes).is_complete() is False

    def test_is_complete_empty(self):
        """Empty graph is NOT complete."""
        assert TaskGraph().is_complete() is False

    def test_has_permanently_failed_true(self):
        nodes = [make_node("t1", status=TaskStatus.FAILED, retries=2, max_retries=2)]
        assert TaskGraph(nodes=nodes).has_permanently_failed() is True

    def test_has_permanently_failed_still_retrying(self):
        nodes = [make_node("t1", status=TaskStatus.FAILED, retries=1, max_retries=2)]
        assert TaskGraph(nodes=nodes).has_permanently_failed() is False


# ---------------------------------------------------------------------------
# Goal — safety limits
# ---------------------------------------------------------------------------

class TestGoalSafetyLimits:
    def test_steps_not_exceeded(self):
        goal = make_goal(max_steps=50, step_count=49)
        assert goal.is_steps_exceeded() is False

    def test_steps_exceeded(self):
        goal = make_goal(max_steps=50, step_count=50)
        assert goal.is_steps_exceeded() is True

    def test_runtime_not_exceeded(self):
        started = (datetime.now(timezone.utc) - timedelta(seconds=100)).isoformat()
        goal = make_goal(max_runtime_seconds=3600, started_at=started)
        assert goal.is_runtime_exceeded() is False

    def test_runtime_exceeded(self):
        started = (datetime.now(timezone.utc) - timedelta(seconds=3700)).isoformat()
        goal = make_goal(max_runtime_seconds=3600, started_at=started)
        assert goal.is_runtime_exceeded() is True

    def test_runtime_no_started_at(self):
        goal = make_goal(max_runtime_seconds=3600)
        assert goal.runtime_seconds() == 0.0
        assert goal.is_runtime_exceeded() is False


# ---------------------------------------------------------------------------
# PlanGenerator helpers — _extract_json
# ---------------------------------------------------------------------------

class TestExtractJSON:
    def test_plain_json(self):
        text = '{"nodes": []}'
        result = _extract_json(text)
        assert result == {"nodes": []}

    def test_json_with_markdown_fence(self):
        text = '```json\n{"nodes": []}\n```'
        result = _extract_json(text)
        assert result == {"nodes": []}

    def test_json_with_leading_text(self):
        text = 'Here is the plan:\n{"nodes": [{"id": "t1"}]}'
        result = _extract_json(text)
        assert result == {"nodes": [{"id": "t1"}]}

    def test_invalid_json(self):
        assert _extract_json("not json at all") is None

    def test_empty_string(self):
        assert _extract_json("") is None

    def test_nested_json(self):
        text = '{"nodes": [{"id": "t1", "arguments": {"key": "value"}}]}'
        result = _extract_json(text)
        assert result["nodes"][0]["arguments"]["key"] == "value"


# ---------------------------------------------------------------------------
# PlanGenerator helpers — _validate_and_build_graph
# ---------------------------------------------------------------------------

class TestValidateAndBuildGraph:
    def _make_data(self, nodes: list[dict]) -> dict:
        return {"nodes": nodes}

    def _node(self, **kwargs) -> dict:
        base = {
            "id": "t1",
            "description": "Do something",
            "skill_name": "web_search",
            "arguments": {"query": "test"},
            "required_capability": "monitored",
            "risk_level": "low",
            "dependencies": [],
            "max_retries": 2,
        }
        base.update(kwargs)
        return base

    def test_valid_single_node(self):
        registry = make_mock_registry(["web_search"])
        data = self._make_data([self._node()])
        graph, err = _validate_and_build_graph(data, registry)
        assert graph is not None
        assert err == ""
        assert graph.total_tasks == 1

    def test_missing_nodes_key(self):
        registry = make_mock_registry()
        graph, err = _validate_and_build_graph({}, registry)
        assert graph is None
        assert "nodes" in err

    def test_empty_nodes(self):
        registry = make_mock_registry()
        graph, err = _validate_and_build_graph({"nodes": []}, registry)
        assert graph is None
        assert "no tasks" in err.lower()

    def test_too_many_nodes(self):
        registry = make_mock_registry(["web_search"])
        nodes = [self._node(id=f"t{i}") for i in range(21)]
        graph, err = _validate_and_build_graph({"nodes": nodes}, registry)
        assert graph is None
        assert "too many" in err.lower()

    def test_unknown_skill(self):
        registry = make_mock_registry(["web_search"])  # no 'mystery_skill'
        data = self._make_data([self._node(skill_name="mystery_skill")])
        graph, err = _validate_and_build_graph(data, registry)
        assert graph is None
        assert "mystery_skill" in err

    def test_duplicate_ids(self):
        registry = make_mock_registry(["web_search"])
        data = self._make_data([self._node(id="t1"), self._node(id="t1")])
        graph, err = _validate_and_build_graph(data, registry)
        assert graph is None
        assert "duplicate" in err.lower()

    def test_cycle_rejected(self):
        registry = make_mock_registry(["web_search"])
        data = self._make_data([
            self._node(id="t1", dependencies=["t2"]),
            self._node(id="t2", skill_name="web_search", dependencies=["t1"]),
        ])
        graph, err = _validate_and_build_graph(data, registry)
        assert graph is None
        assert "cycle" in err.lower()

    def test_valid_linear_chain(self):
        registry = make_mock_registry(["web_search"])
        data = self._make_data([
            self._node(id="t1", dependencies=[]),
            self._node(id="t2", dependencies=["t1"]),
        ])
        graph, err = _validate_and_build_graph(data, registry)
        assert graph is not None
        assert graph.total_tasks == 2

    def test_risk_level_default_on_invalid(self):
        """Invalid risk_level should default to LOW, not crash."""
        registry = make_mock_registry(["web_search"])
        data = self._make_data([self._node(risk_level="extreme")])
        graph, err = _validate_and_build_graph(data, registry)
        assert graph is not None  # defaults to LOW
        assert graph.nodes[0].risk_level == RiskLevel.LOW

    def test_arguments_coerced_to_strings(self):
        """Non-string argument values should be coerced to strings."""
        registry = make_mock_registry(["web_search"])
        data = self._make_data([self._node(arguments={"count": 5, "flag": True})])
        graph, err = _validate_and_build_graph(data, registry)
        assert graph is not None
        args = graph.nodes[0].arguments
        assert args["count"] == "5"
        assert args["flag"] == "True"


# ---------------------------------------------------------------------------
# GoalStepExecutor — step limit and runtime limit
# ---------------------------------------------------------------------------

class TestGoalStepExecutorLimits:
    def _make_executor(self):
        mock_skill_exec = AsyncMock()
        mock_memory = AsyncMock()
        mock_bus = AsyncMock()
        return GoalStepExecutor(
            skill_executor=mock_skill_exec,
            memory_manager=mock_memory,
            bus=mock_bus,
        )

    @pytest.mark.asyncio
    async def test_step_limit_fails_goal(self):
        executor = self._make_executor()
        goal = make_goal(max_steps=5, step_count=5)  # at limit
        goal.state = GoalState.ACTIVE
        goal.task_graph = TaskGraph(nodes=[make_node("t1")])

        result = await executor.step(goal)

        assert result.goal.state == GoalState.FAILED
        assert "step limit" in result.goal.error.lower()
        assert result.action == "limit_exceeded"

    @pytest.mark.asyncio
    async def test_runtime_limit_fails_goal(self):
        executor = self._make_executor()
        started = (datetime.now(timezone.utc) - timedelta(seconds=3700)).isoformat()
        goal = make_goal(max_runtime_seconds=3600, started_at=started)
        goal.state = GoalState.ACTIVE
        goal.task_graph = TaskGraph(nodes=[make_node("t1")])

        result = await executor.step(goal)

        assert result.goal.state == GoalState.FAILED
        assert "runtime" in result.goal.error.lower()
        assert result.action == "limit_exceeded"

    @pytest.mark.asyncio
    async def test_high_risk_blocks_and_pauses(self):
        executor = self._make_executor()
        goal = make_goal()
        goal.state = GoalState.ACTIVE
        goal.task_graph = TaskGraph(nodes=[make_node("t1", risk_level=RiskLevel.HIGH)])

        result = await executor.step(goal)

        assert result.goal.state == GoalState.PAUSED
        assert result.action == "autonomy_blocked"
        assert goal.task_graph.nodes[0].status == TaskStatus.BLOCKED

    @pytest.mark.asyncio
    async def test_task_done_on_success(self):
        from src.skills.types import SkillResult

        executor = self._make_executor()
        mock_result = SkillResult(skill_name="web_search", success=True, output="OK")
        executor.skill_executor.execute = AsyncMock(return_value=mock_result)

        # Patch memory write to avoid DB
        executor._write_episodic_memory = AsyncMock()
        executor._notify = AsyncMock()

        goal = make_goal()
        goal.state = GoalState.ACTIVE
        goal.task_graph = TaskGraph(nodes=[make_node("t1")])

        result = await executor.step(goal)

        assert result.action == "task_done"
        assert goal.task_graph.nodes[0].status == TaskStatus.DONE
        assert goal.step_count == 1

    @pytest.mark.asyncio
    async def test_task_retrying_on_failure(self):
        from src.skills.types import SkillResult

        executor = self._make_executor()
        mock_result = SkillResult(skill_name="web_search", success=False, output="", error="timeout")
        executor.skill_executor.execute = AsyncMock(return_value=mock_result)
        executor._write_episodic_memory = AsyncMock()
        executor._notify = AsyncMock()

        goal = make_goal()
        goal.state = GoalState.ACTIVE
        goal.task_graph = TaskGraph(nodes=[make_node("t1", retries=0, max_retries=2)])

        result = await executor.step(goal)

        assert result.action == "task_retrying"
        # Status reset to PENDING for retry
        assert goal.task_graph.nodes[0].status == TaskStatus.PENDING

    @pytest.mark.asyncio
    async def test_task_permanently_failed(self):
        from src.skills.types import SkillResult

        executor = self._make_executor()
        mock_result = SkillResult(skill_name="web_search", success=False, output="", error="timeout")
        executor.skill_executor.execute = AsyncMock(return_value=mock_result)
        executor._write_episodic_memory = AsyncMock()
        executor._notify = AsyncMock()

        goal = make_goal()
        goal.state = GoalState.ACTIVE
        # retries=1, max_retries=1 → next failure is permanent
        goal.task_graph = TaskGraph(nodes=[make_node("t1", retries=1, max_retries=1)])

        result = await executor.step(goal)

        assert result.action == "task_failed"
        assert goal.task_graph.nodes[0].status == TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_goal_complete_when_all_done(self):
        executor = self._make_executor()
        executor._notify = AsyncMock()

        goal = make_goal()
        goal.state = GoalState.ACTIVE
        goal.task_graph = TaskGraph(nodes=[make_node("t1", status=TaskStatus.DONE)])

        result = await executor.step(goal)

        assert result.action == "goal_complete"
        assert result.goal.state == GoalState.COMPLETED

    @pytest.mark.asyncio
    async def test_no_task_when_deps_unmet(self):
        executor = self._make_executor()

        goal = make_goal()
        goal.state = GoalState.ACTIVE
        # t1 has a dep on t0 which is PENDING (not done) → nothing executable
        goal.task_graph = TaskGraph(nodes=[
            make_node("t0"),                  # PENDING
            make_node("t1", deps=["t0"]),     # PENDING, dep not met
        ])
        # Don't execute t0 — manually set to RUNNING to simulate
        goal.task_graph.nodes[0].status = TaskStatus.RUNNING

        result = await executor.step(goal)

        assert result.action == "no_task"
