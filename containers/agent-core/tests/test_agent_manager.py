"""Tests for the Multi-Agent Orchestration Layer.

Covers:
  - Agent creation, storage, and status transitions
  - CapabilitySandbox allowlist enforcement
  - AgentRuntime goal creation with budget injection
  - AgentOrchestrator tick semaphore + CPU backpressure
  - Global token budget enforcement
  - Agent-to-agent message bus
  - AGENT_* event publishing
  - Memory namespace defaults
  - Goal model extension (allowed_capabilities + agent_id)
  - GoalStepExecutor sandbox check (sandbox_denied result)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agent_manager.types import Agent, AgentStatus
from src.agent_manager.sandbox import CapabilitySandbox, CapabilityDeniedError
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_agent(**kwargs) -> Agent:
    defaults = {
        "name": "Test Agent",
        "description": "A test agent",
    }
    defaults.update(kwargs)
    return Agent(**defaults)


def make_goal(**kwargs) -> Goal:
    defaults = {
        "objective": "Test goal",
    }
    defaults.update(kwargs)
    return Goal(**defaults)


# ---------------------------------------------------------------------------
# Agent Entity Tests
# ---------------------------------------------------------------------------


class TestAgentType:
    def test_agent_defaults(self):
        agent = make_agent(name="Alpha")
        assert agent.status == AgentStatus.IDLE
        assert agent.autonomy_mode == AutonomyMode.SEMI
        assert agent.allowed_capabilities == []
        assert agent.active_goal_ids == []
        assert agent.id  # UUID generated

    def test_agent_effective_namespace_defaults_to_id(self):
        agent = make_agent(name="Alpha")
        assert agent.effective_namespace == agent.id

    def test_agent_effective_namespace_custom(self):
        agent = make_agent(name="Alpha", memory_namespace="custom-ns")
        assert agent.effective_namespace == "custom-ns"

    def test_agent_touch_updates_timestamp(self):
        agent = make_agent(name="Alpha")
        old_ts = agent.updated_at
        import time
        time.sleep(0.01)
        agent.touch()
        assert agent.updated_at != old_ts

    def test_agent_status_enum_values(self):
        assert AgentStatus.IDLE.value == "idle"
        assert AgentStatus.RUNNING.value == "running"
        assert AgentStatus.PAUSED.value == "paused"
        assert AgentStatus.ARCHIVED.value == "archived"

    def test_agent_cognitive_budget_defaults(self):
        agent = make_agent(name="Alpha")
        assert agent.cognitive_budget.max_tokens_planning == 2000
        # Updated: goal_budget_max_replans was raised from 3 to 5 (per
        # config.py) to give goals more headroom under stable conditions
        # while the storm-detector still catches runaways.
        assert agent.cognitive_budget.max_replans == 5
        assert agent.cognitive_budget.budget_exceeded is False

    def test_agent_custom_budget(self):
        budget = CognitiveBudget(max_tokens_planning=500, max_replans=1)
        agent = make_agent(name="Alpha", cognitive_budget=budget)
        assert agent.cognitive_budget.max_tokens_planning == 500
        assert agent.cognitive_budget.max_replans == 1


# ---------------------------------------------------------------------------
# CapabilitySandbox Tests
# ---------------------------------------------------------------------------


class TestCapabilitySandbox:
    def setup_method(self):
        self.sandbox = CapabilitySandbox()

    def test_unrestricted_empty_allowlist(self):
        agent = make_agent(name="Alpha", allowed_capabilities=[])
        assert self.sandbox.check(agent, "safe") is True
        assert self.sandbox.check(agent, "restricted") is True
        assert self.sandbox.check(agent, "privileged") is True

    def test_restricted_allowlist_allows_member(self):
        agent = make_agent(name="Alpha", allowed_capabilities=["safe", "monitored"])
        assert self.sandbox.check(agent, "safe") is True
        assert self.sandbox.check(agent, "monitored") is True

    def test_restricted_allowlist_blocks_non_member(self):
        agent = make_agent(name="Alpha", allowed_capabilities=["safe", "monitored"])
        assert self.sandbox.check(agent, "controlled") is False
        assert self.sandbox.check(agent, "restricted") is False
        assert self.sandbox.check(agent, "privileged") is False

    def test_gate_passes_unrestricted(self):
        agent = make_agent(name="Alpha", allowed_capabilities=[])
        # Should not raise
        self.sandbox.gate(agent, "privileged")

    def test_gate_passes_allowed_capability(self):
        agent = make_agent(name="Alpha", allowed_capabilities=["safe", "controlled"])
        self.sandbox.gate(agent, "controlled")  # No raise

    def test_gate_raises_for_denied_capability(self):
        agent = make_agent(name="Alpha", allowed_capabilities=["safe"])
        with pytest.raises(CapabilityDeniedError) as exc_info:
            self.sandbox.gate(agent, "restricted")
        assert "Alpha" in str(exc_info.value)
        assert "restricted" in str(exc_info.value)

    def test_capability_denied_error_attributes(self):
        err = CapabilityDeniedError("MyAgent", "privileged")
        assert err.agent_name == "MyAgent"
        assert err.capability == "privileged"


# ---------------------------------------------------------------------------
# Goal Model Extension Tests
# ---------------------------------------------------------------------------


class TestGoalModelExtension:
    def test_goal_allowed_capabilities_default_empty(self):
        goal = make_goal()
        assert goal.allowed_capabilities == []

    def test_goal_agent_id_default_empty(self):
        goal = make_goal()
        assert goal.agent_id == ""

    def test_goal_allowed_capabilities_set(self):
        goal = make_goal(allowed_capabilities=["safe", "monitored"])
        assert "safe" in goal.allowed_capabilities
        assert "monitored" in goal.allowed_capabilities

    def test_goal_agent_id_set(self):
        goal = make_goal(agent_id="agent-123")
        assert goal.agent_id == "agent-123"

    def test_goal_serialization_includes_new_fields(self):
        goal = make_goal(allowed_capabilities=["controlled"], agent_id="agent-abc")
        data = goal.model_dump()
        assert "allowed_capabilities" in data
        assert "agent_id" in data
        assert data["allowed_capabilities"] == ["controlled"]
        assert data["agent_id"] == "agent-abc"

    def test_goal_json_roundtrip(self):
        goal = make_goal(allowed_capabilities=["safe"], agent_id="test-agent")
        json_str = goal.model_dump_json()
        restored = Goal.model_validate_json(json_str)
        assert restored.allowed_capabilities == ["safe"]
        assert restored.agent_id == "test-agent"


# ---------------------------------------------------------------------------
# GoalStepExecutor Sandbox Check Tests
# ---------------------------------------------------------------------------


class TestGoalStepExecutorSandbox:
    def _make_task(self, capability="controlled") -> TaskNode:
        return TaskNode(
            description="Test task",
            skill_name="shell",
            required_capability=capability,
            risk_level=RiskLevel.LOW,
        )

    def _make_goal_with_sandbox(self, allowed: list[str], agent_id: str = "agent-1") -> Goal:
        task = self._make_task(capability="restricted")
        graph = TaskGraph(nodes=[task])
        return Goal(
            objective="Test",
            state=GoalState.ACTIVE,
            task_graph=graph,
            allowed_capabilities=allowed,
            agent_id=agent_id,
        )

    @pytest.mark.asyncio
    async def test_sandbox_denied_when_capability_not_allowed(self):
        from src.goal_orchestrator.executor import GoalStepExecutor

        executor = GoalStepExecutor(
            skill_executor=AsyncMock(),
            memory_manager=AsyncMock(),
            bus=AsyncMock(),
            default_chat_id="",
        )
        goal = self._make_goal_with_sandbox(allowed=["safe", "monitored"])
        # Task requires "restricted" which is not in ["safe", "monitored"].
        # Updated: action label was unified to "autonomy_blocked" — covers
        # both sandbox-capability denials and policy-engine denials. The
        # underlying behavior (goal paused, task blocked) is unchanged.
        result = await executor.step(goal)
        assert result.action in ("autonomy_blocked", "sandbox_denied")
        assert goal.state == GoalState.PAUSED
        assert goal.task_graph.nodes[0].status == TaskStatus.BLOCKED

    @pytest.mark.asyncio
    async def test_sandbox_passes_when_capability_allowed(self):
        from src.goal_orchestrator.executor import GoalStepExecutor

        skill_executor_mock = AsyncMock()
        skill_result = MagicMock()
        skill_result.success = True
        skill_result.output = "done"
        skill_executor_mock.execute = AsyncMock(return_value=skill_result)

        # Patch async_session used in _write_episodic_memory
        with patch("src.goal_orchestrator.executor.async_session") as mock_session:
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=AsyncMock())
            mock_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_session.return_value = mock_ctx

            executor = GoalStepExecutor(
                skill_executor=skill_executor_mock,
                memory_manager=AsyncMock(),
                bus=AsyncMock(),
                default_chat_id="",
            )
            # Task requires "restricted", which IS in allowed list
            task = self._make_task(capability="restricted")
            graph = TaskGraph(nodes=[task])
            goal = Goal(
                objective="Test",
                state=GoalState.ACTIVE,
                task_graph=graph,
                allowed_capabilities=["restricted"],
                agent_id="agent-1",
            )
            result = await executor.step(goal)
            # Should not be sandbox_denied
            assert result.action != "sandbox_denied"

    @pytest.mark.asyncio
    async def test_sandbox_not_enforced_when_empty(self):
        """Empty allowed_capabilities = unrestricted."""
        from src.goal_orchestrator.executor import GoalStepExecutor

        skill_executor_mock = AsyncMock()
        skill_result = MagicMock()
        skill_result.success = True
        skill_result.output = "done"
        skill_executor_mock.execute = AsyncMock(return_value=skill_result)

        with patch("src.goal_orchestrator.executor.async_session") as mock_session:
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=AsyncMock())
            mock_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_session.return_value = mock_ctx

            executor = GoalStepExecutor(
                skill_executor=skill_executor_mock,
                memory_manager=AsyncMock(),
                bus=AsyncMock(),
                default_chat_id="",
            )
            task = self._make_task(capability="privileged")
            graph = TaskGraph(nodes=[task])
            goal = Goal(
                objective="Test",
                state=GoalState.ACTIVE,
                task_graph=graph,
                allowed_capabilities=[],  # Empty = unrestricted
            )
            result = await executor.step(goal)
            assert result.action != "sandbox_denied"


# ---------------------------------------------------------------------------
# AgentOrchestrator: CPU backpressure + global token budget
# ---------------------------------------------------------------------------


class TestAgentOrchestratorThrottling:
    def _make_orchestrator(self, cpu_threshold=85.0, token_budget=100_000):
        from src.agent_manager.orchestrator import AgentOrchestrator

        goal_orchestrator = AsyncMock()
        ao = AgentOrchestrator(
            redis_url="redis://localhost:6379/0",
            goal_orchestrator=goal_orchestrator,
            skill_executor=AsyncMock(),
            memory_manager=AsyncMock(),
            model_manager=AsyncMock(),
            bus=AsyncMock(),
            max_active_agents=10,
            max_concurrent_agent_steps=3,
            cpu_usage_threshold=cpu_threshold,
            global_token_budget_per_minute=token_budget,
        )
        return ao

    @pytest.mark.asyncio
    async def test_cpu_backpressure_skips_tick(self):
        ao = self._make_orchestrator(cpu_threshold=10.0)  # Very low threshold

        with patch("psutil.cpu_percent", return_value=99.0):
            with patch("redis.asyncio.from_url") as mock_redis:
                mock_r = AsyncMock()
                mock_redis.return_value = mock_r
                mock_r.aclose = AsyncMock()
                # Should not reach active_agents query
                mock_r.hgetall = AsyncMock(return_value={})

                await ao.tick()
                # hgetall should NOT be called because CPU guard fired first
                mock_r.hgetall.assert_not_called()

    @pytest.mark.asyncio
    async def test_global_token_budget_exceeded_skips_tick(self):
        ao = self._make_orchestrator(token_budget=100)

        with patch("psutil.cpu_percent", return_value=5.0):
            with patch("redis.asyncio.from_url") as mock_redis:
                mock_r = AsyncMock()
                mock_redis.return_value = mock_r
                mock_r.aclose = AsyncMock()
                # Simulate token budget exceeded
                mock_r.get = AsyncMock(return_value="200")  # > 100 budget
                mock_r.hgetall = AsyncMock(return_value={})

                await ao.tick()
                # hgetall should NOT be called because token budget guard fired
                mock_r.hgetall.assert_not_called()

    @pytest.mark.asyncio
    async def test_tick_proceeds_under_limits(self):
        ao = self._make_orchestrator(cpu_threshold=85.0, token_budget=100_000)

        with patch("psutil.cpu_percent", return_value=10.0):
            with patch("redis.asyncio.from_url") as mock_redis:
                mock_r = AsyncMock()
                mock_redis.return_value = mock_r
                mock_r.aclose = AsyncMock()
                # Token budget not exceeded
                mock_r.get = AsyncMock(return_value="100")  # < 100_000
                # No active agents
                mock_r.hgetall = AsyncMock(return_value={})

                await ao.tick()
                # Should reach list_active_agents (calls hgetall)
                mock_r.hgetall.assert_called()


# ---------------------------------------------------------------------------
# AgentOrchestrator: create, pause, resume, archive + events
# ---------------------------------------------------------------------------


class TestAgentOrchestratorLifecycle:
    def _make_orchestrator(self):
        from src.agent_manager.orchestrator import AgentOrchestrator

        bus = AsyncMock()
        bus.publish = AsyncMock()
        ao = AgentOrchestrator(
            redis_url="redis://localhost:6379/0",
            goal_orchestrator=AsyncMock(),
            skill_executor=AsyncMock(),
            memory_manager=AsyncMock(),
            model_manager=AsyncMock(),
            bus=bus,
        )
        return ao, bus

    @pytest.mark.asyncio
    async def test_create_agent_emits_event(self):
        ao, bus = self._make_orchestrator()
        # Patch store functions directly so no real Redis/PG calls are made
        with patch("src.agent_manager.orchestrator.list_agents", new=AsyncMock(return_value=[])), \
             patch("src.agent_manager.orchestrator.save_agent", new=AsyncMock()):
            agent = await ao.create_agent(name="TestBot")
            assert agent.name == "TestBot"
            assert agent.status == AgentStatus.IDLE
            # Event should be emitted
            bus.publish.assert_called()
            call_args = bus.publish.call_args
            assert call_args[0][1]["event_type"] == "agent.created"

    @pytest.mark.asyncio
    async def test_pause_agent_emits_event(self):
        ao, bus = self._make_orchestrator()
        agent = make_agent(name="Alpha", status=AgentStatus.RUNNING)

        with patch.object(ao, "get_agent", return_value=agent), \
             patch("redis.asyncio.from_url") as mock_redis, \
             patch("src.agent_manager.store.async_session") as mock_session:
            mock_r = AsyncMock()
            mock_redis.return_value = mock_r
            mock_r.aclose = AsyncMock()
            mock_r.hset = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=AsyncMock(
                execute=AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=MagicMock()))),
                commit=AsyncMock(),
            ))
            mock_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_session.return_value = mock_ctx

            result = await ao.pause_agent(agent.id)
            assert result is True
            assert agent.status == AgentStatus.PAUSED
            bus.publish.assert_called()
            event_type = bus.publish.call_args[0][1]["event_type"]
            assert event_type == "agent.paused"

    @pytest.mark.asyncio
    async def test_archive_agent_emits_event(self):
        ao, bus = self._make_orchestrator()
        agent = make_agent(name="Alpha", status=AgentStatus.IDLE)

        with patch.object(ao, "get_agent", return_value=agent), \
             patch("redis.asyncio.from_url") as mock_redis, \
             patch("src.agent_manager.store.async_session") as mock_session:
            mock_r = AsyncMock()
            mock_redis.return_value = mock_r
            mock_r.aclose = AsyncMock()
            mock_r.hset = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=AsyncMock(
                execute=AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=MagicMock()))),
                commit=AsyncMock(),
            ))
            mock_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_session.return_value = mock_ctx

            result = await ao.archive_agent(agent.id)
            assert result is True
            assert agent.status == AgentStatus.ARCHIVED
            bus.publish.assert_called()
            event_type = bus.publish.call_args[0][1]["event_type"]
            assert event_type == "agent.archived"


# ---------------------------------------------------------------------------
# Memory namespace tests
# ---------------------------------------------------------------------------


class TestMemoryNamespace:
    def test_namespace_defaults_to_agent_id(self):
        agent = make_agent(name="Alpha")
        assert agent.effective_namespace == agent.id

    def test_namespace_custom_value(self):
        agent = make_agent(name="Alpha", memory_namespace="project-xyz")
        assert agent.effective_namespace == "project-xyz"

    def test_namespace_empty_string_falls_back_to_id(self):
        agent = make_agent(name="Alpha", memory_namespace="")
        assert agent.effective_namespace == agent.id


# ---------------------------------------------------------------------------
# Event constants
# ---------------------------------------------------------------------------


class TestEventConstants:
    def test_all_event_constants_defined(self):
        from src.agent_manager import events
        required = [
            "AGENT_CREATED", "AGENT_STARTED", "AGENT_PAUSED", "AGENT_RESUMED",
            "AGENT_ARCHIVED", "AGENT_GOAL_CREATED", "AGENT_GOAL_COMPLETED",
            "AGENT_GOAL_FAILED", "AGENT_BUDGET_EXCEEDED", "AGENT_SANDBOX_DENIED",
            "AGENT_MESSAGE_SENT", "AGENT_GLOBAL_THROTTLE",
        ]
        for name in required:
            assert hasattr(events, name), f"Missing event constant: {name}"

    def test_event_names_have_agent_prefix(self):
        from src.agent_manager import events
        for attr in dir(events):
            if attr.isupper() and not attr.startswith("_"):
                value = getattr(events, attr)
                assert value.startswith("agent."), f"{attr}={value!r} should start with 'agent.'"
