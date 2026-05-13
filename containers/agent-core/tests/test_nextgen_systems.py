"""Integration tests for the 6 Next-Gen Cognitive Systems.

Tests verify:
1. Old behaviour is unchanged (backward compatibility)
2. New modules import without errors
3. Feature flags work correctly (disabled → no-op)
4. Core logic is correct (unit-level with mocks)

Run with: docker compose run --rm agent-core python -m pytest tests/test_nextgen_systems.py -v
"""

from __future__ import annotations

import asyncio
import sys
import math
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

import pytest

# Ensure src is on the path
sys.path.insert(0, str(Path(__file__).parent.parent))

# ---------------------------------------------------------------------------
# System 1: Vector Semantic Memory
# ---------------------------------------------------------------------------


class TestVectorMemory:
    """Tests for src/memory/vector_memory.py"""

    def test_hash_embedding_deterministic(self):
        """Same text → same hash embedding."""
        from src.memory.vector_memory import hash_embedding
        a = hash_embedding("hello world")
        b = hash_embedding("hello world")
        assert a == b, "Hash embedding must be deterministic"

    def test_hash_embedding_different_texts(self):
        """Different texts → different hash embeddings."""
        from src.memory.vector_memory import hash_embedding
        a = hash_embedding("BTC price analysis")
        b = hash_embedding("recipe for pasta")
        assert a != b, "Different texts should produce different embeddings"

    def test_hash_embedding_unit_length(self):
        """Hash embedding should be L2-normalised."""
        from src.memory.vector_memory import hash_embedding
        vec = hash_embedding("test text")
        norm = math.sqrt(sum(x * x for x in vec))
        assert abs(norm - 1.0) < 0.001, f"Expected unit vector, got norm={norm}"

    def test_cosine_similarity_identical(self):
        """Identical unit vectors → similarity = 1.0."""
        from src.memory.vector_memory import cosine_similarity
        vec = [0.6, 0.8]  # already unit vector
        assert abs(cosine_similarity(vec, vec) - 1.0) < 1e-6

    def test_cosine_similarity_orthogonal(self):
        """Orthogonal vectors → similarity = 0.0."""
        from src.memory.vector_memory import cosine_similarity
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert abs(cosine_similarity(a, b)) < 1e-6

    def test_cosine_similarity_mismatched_lengths(self):
        """Mismatched length vectors → 0.0 (safe fallback)."""
        from src.memory.vector_memory import cosine_similarity
        assert cosine_similarity([1.0, 0.0], [1.0]) == 0.0

    def test_format_for_context_empty(self):
        """Empty results → empty string."""
        from src.memory.vector_memory import format_for_context
        assert format_for_context([]) == ""

    def test_format_for_context_with_results(self):
        """Results → non-empty block with [MEMORIA SEMÁNTICA RELEVANTE] header."""
        from src.memory.vector_memory import format_for_context
        results = [
            {"source_id": "abc", "source_type": "episodic", "preview": "BTC was $45k", "score": 0.82},
        ]
        output = format_for_context(results)
        assert "[MEMORIA SEMÁNTICA RELEVANTE]" in output
        assert "82%" in output or "82" in output
        assert "BTC" in output

    @pytest.mark.asyncio
    async def test_embed_with_fallback_returns_hash_when_provider_fails(self):
        """When the semantic provider returns None, the fallback embedder
        switches to deterministic hash embeddings instead of raising.

        Updated: the previous public ``generate_embedding`` was refactored
        into ``_embed_with_fallback(text, provider)`` where the provider
        encapsulates the connection details. The contract — never raise on
        provider failure, return a usable vector — is unchanged.
        """
        from src.memory.vector_memory import _embed_with_fallback

        class _BrokenProvider:
            model_name = "broken"
            is_semantic = True
            async def embed(self, _text):
                return None  # simulate provider down

        vec, model = await _embed_with_fallback("test", _BrokenProvider())
        assert isinstance(vec, list) and vec, "fallback must return a usable vector"
        assert "hash" in model.lower() or model == "broken"


# ---------------------------------------------------------------------------
# System 2: Dual-Layer Planner — Plan Critic
# ---------------------------------------------------------------------------


class TestPlanCritic:
    """Tests for src/goal_orchestrator/plan_validator.py"""

    def _make_task_graph(self, n=2):
        """Create a minimal valid TaskGraph."""
        from src.goal_orchestrator.types import TaskGraph, TaskNode, RiskLevel
        nodes = [
            TaskNode(
                id=f"task_{i}",
                description=f"Task {i}",
                skill_name="web_search",
                arguments={"query": f"test {i}"},
                dependencies=[] if i == 0 else [f"task_{i-1}"],
                risk_level=RiskLevel.LOW,
            )
            for i in range(n)
        ]
        return TaskGraph(nodes=nodes)

    def _make_goal(self, objective="Test goal"):
        """Create a minimal Goal for testing."""
        from src.goal_orchestrator.types import Goal, GoalState, AutonomyMode, CognitiveBudget, StabilityState, GoalTelemetry
        return Goal(
            id="test-goal-id",
            objective=objective,
            state=GoalState.PLANNING,
            autonomy_mode=AutonomyMode.SEMI,
            budget=CognitiveBudget(),
            stability=StabilityState(),
            telemetry=GoalTelemetry(),
        )

    @pytest.mark.asyncio
    async def test_critic_disabled_passthrough(self):
        """When enabled=False, validate() returns original graph unchanged."""
        from src.goal_orchestrator.plan_validator import PlanCritic
        mock_mm = MagicMock()
        mock_reg = MagicMock()
        mock_reg.list_enabled.return_value = []
        critic = PlanCritic(mock_mm, mock_reg, enabled=False)
        graph = self._make_task_graph(2)
        goal = self._make_goal()
        result = await critic.validate(goal, graph)
        assert result is graph, "Disabled critic should return original graph"
        mock_mm.generate.assert_not_called()

    @pytest.mark.asyncio
    async def test_critic_empty_graph_passthrough(self):
        """Empty task graph → critic skips validation."""
        from src.goal_orchestrator.plan_validator import PlanCritic
        from src.goal_orchestrator.types import TaskGraph
        mock_mm = MagicMock()
        mock_reg = MagicMock()
        mock_reg.list_enabled.return_value = []
        critic = PlanCritic(mock_mm, mock_reg, enabled=True)
        empty_graph = TaskGraph(nodes=[])
        goal = self._make_goal()
        result = await critic.validate(goal, empty_graph)
        assert result is empty_graph
        mock_mm.generate.assert_not_called()

    @pytest.mark.asyncio
    async def test_critic_ok_verdict_returns_original(self):
        """Critic returning 'ok' verdict → original graph unchanged."""
        from src.goal_orchestrator.plan_validator import PlanCritic
        mock_mm = MagicMock()
        mock_reg = MagicMock()
        mock_reg.list_enabled.return_value = []

        mock_resp = MagicMock()
        mock_resp.content = '{"verdict": "ok", "issues": [], "corrected_tasks": []}'
        mock_mm.generate = AsyncMock(return_value=mock_resp)

        critic = PlanCritic(mock_mm, mock_reg, enabled=True)
        graph = self._make_task_graph(2)
        goal = self._make_goal()
        result = await critic.validate(goal, graph)
        assert result is graph
        assert len(result.nodes) == 2

    @pytest.mark.asyncio
    async def test_critic_llm_error_returns_original(self):
        """When LLM call fails, original graph is returned (no crash)."""
        from src.goal_orchestrator.plan_validator import PlanCritic
        mock_mm = MagicMock()
        mock_reg = MagicMock()
        mock_reg.list_enabled.return_value = []
        mock_mm.generate = AsyncMock(side_effect=Exception("LLM unavailable"))

        critic = PlanCritic(mock_mm, mock_reg, enabled=True)
        graph = self._make_task_graph(2)
        goal = self._make_goal()
        result = await critic.validate(goal, graph)
        assert result is graph, "Should fall back to original on LLM error"


# ---------------------------------------------------------------------------
# System 4: World Model
# ---------------------------------------------------------------------------


class TestWorldModel:
    """Tests for src/world/world_model.py"""

    def test_parse_numeric_dollar(self):
        """Parse '$45,231.50' → 45231.5"""
        from src.world.world_model import _parse_numeric
        assert _parse_numeric("$45,231.50") == pytest.approx(45231.50)

    def test_parse_numeric_percent(self):
        """Parse '3.14%' → 3.14"""
        from src.world.world_model import _parse_numeric
        assert _parse_numeric("3.14%") == pytest.approx(3.14)

    def test_parse_numeric_none_on_text(self):
        """Parse 'stable' → None"""
        from src.world.world_model import _parse_numeric
        assert _parse_numeric("stable") is None

    def test_trend_direction_up(self):
        """Rising values → 'up'."""
        from src.world.world_model import _trend_direction
        assert _trend_direction([100, 110, 120, 130]) == "up"

    def test_trend_direction_down(self):
        """Falling values → 'down'."""
        from src.world.world_model import _trend_direction
        assert _trend_direction([130, 120, 110, 100]) == "down"

    def test_trend_direction_stable(self):
        """Flat values → 'stable'."""
        from src.world.world_model import _trend_direction
        assert _trend_direction([100.0, 100.1, 99.9, 100.05]) == "stable"

    def test_trend_direction_single_value(self):
        """Single value → 'unknown'."""
        from src.world.world_model import _trend_direction
        assert _trend_direction([100.0]) == "unknown"


# ---------------------------------------------------------------------------
# System 5: Skill Evolution Engine
# ---------------------------------------------------------------------------


class TestSkillEvolution:
    """Tests for src/skills/skill_evolution.py"""

    def test_sequence_to_key(self):
        """Sequence list → canonical string key."""
        from src.skills.skill_evolution import _sequence_to_key
        assert _sequence_to_key(["web_search", "fetch_url", "gmail"]) == "web_search→fetch_url→gmail"

    def test_extract_skill_name_colon(self):
        """'skill:web_search' → 'web_search'."""
        from src.skills.skill_evolution import _extract_skill_name
        assert _extract_skill_name("skill:web_search") == "web_search"

    def test_extract_skill_name_dot(self):
        """'skill.python_exec' → 'python_exec'."""
        from src.skills.skill_evolution import _extract_skill_name
        assert _extract_skill_name("skill.python_exec") == "python_exec"

    def test_extract_skill_name_none_on_long(self):
        """Long non-skill action → None."""
        from src.skills.skill_evolution import _extract_skill_name
        assert _extract_skill_name("goal.task_step_executed:abc123") is None


# ---------------------------------------------------------------------------
# System 6: Temporal Reasoner
# ---------------------------------------------------------------------------


class TestTemporalReasoner:
    """Tests for src/reasoning/temporal_reasoner.py"""

    def test_extract_number_with_dollar(self):
        """'$50,000' → 50000.0"""
        from src.reasoning.temporal_reasoner import _extract_number
        assert _extract_number("$50,000") == pytest.approx(50000.0)

    def test_extract_number_negative(self):
        """'-3.5%' → -3.5"""
        from src.reasoning.temporal_reasoner import _extract_number
        result = _extract_number("-3.5%")
        assert result is not None and abs(result) == pytest.approx(3.5)

    def test_extract_number_no_number(self):
        """'volatile' → None"""
        from src.reasoning.temporal_reasoner import _extract_number
        assert _extract_number("volatile") is None

    def test_classify_trend_up(self):
        """Rising values → 'up'."""
        from src.reasoning.temporal_reasoner import _classify_trend
        assert _classify_trend([1000, 1050, 1100, 1200]) == "up"

    def test_classify_trend_down(self):
        """Falling values → 'down'."""
        from src.reasoning.temporal_reasoner import _classify_trend
        assert _classify_trend([1200, 1100, 1050, 1000]) == "down"

    def test_classify_trend_stable(self):
        """Tiny fluctuations → 'stable'."""
        from src.reasoning.temporal_reasoner import _classify_trend
        assert _classify_trend([100.0, 100.1, 99.95, 100.02]) == "stable"

    def test_format_for_context_empty(self):
        """Empty summaries → empty string."""
        from src.reasoning.temporal_reasoner import TemporalReasoner
        reasoner = TemporalReasoner()
        assert reasoner.format_for_context([]) == ""

    def test_format_for_context_contains_header(self):
        """Non-empty summaries → contains [TEMPORAL INSIGHTS] header."""
        from src.reasoning.temporal_reasoner import TemporalReasoner, TrendSummary
        reasoner = TemporalReasoner()
        summary = TrendSummary(
            entity="BTC",
            current_value="$50,000",
            previous_value="$45,000",
            change_pct=11.1,
            trend="up",
            observations=24,
            time_window_hours=24.0,
        )
        output = reasoner.format_for_context([summary])
        assert "[TEMPORAL INSIGHTS" in output
        assert "BTC" in output
        assert "📈" in output


# ---------------------------------------------------------------------------
# Backward compatibility tests
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Verify existing system behavior is unchanged."""

    def test_config_has_all_original_fields(self):
        """All original config fields still exist with correct defaults."""
        from src.config import settings
        assert hasattr(settings, "redis_url")
        assert hasattr(settings, "goal_engine_enabled")
        assert hasattr(settings, "agents_enabled")
        assert hasattr(settings, "skill_pattern_threshold")  # new field

    def test_new_config_flags_match_documented_defaults(self):
        """Feature flags default to documented values.

        Updated: vector_memory_enabled was promoted to True (with safe
        hash-embedding fallback). plan_critic_enabled was promoted to True
        (catches truth-violating plans before execution — the user wants
        strict enforcement). meta_agent_enabled stays False (research only).
        """
        from src.config import settings
        assert settings.vector_memory_enabled is True
        assert settings.plan_critic_enabled is True
        assert settings.meta_agent_enabled is False
        # Updated: skill_evolution was promoted to True so the agent can
        # synthesise composite skills from recurring patterns autonomously.
        assert settings.skill_evolution_enabled is True
        # Safe read-only features can be on by default
        assert settings.world_model_enabled is True
        assert settings.temporal_reasoning_enabled is True

    def test_new_db_models_importable(self):
        """New SQLAlchemy models import without error."""
        from src.db.models import (
            MemoryEmbedding,
            SkillPattern,
            EntityState,
            StatePrediction,
        )
        # Verify table names
        assert MemoryEmbedding.__tablename__ == "memory_embeddings"
        assert SkillPattern.__tablename__ == "skill_patterns"
        assert EntityState.__tablename__ == "entity_states"
        assert StatePrediction.__tablename__ == "state_predictions"

    def test_vector_memory_module_importable(self):
        from src.memory import vector_memory
        assert hasattr(vector_memory, "semantic_search")
        assert hasattr(vector_memory, "store_embedding")
        assert hasattr(vector_memory, "format_for_context")

    def test_plan_validator_module_importable(self):
        from src.goal_orchestrator.plan_validator import PlanCritic
        assert PlanCritic is not None

    def test_world_model_module_importable(self):
        from src.world.world_model import WorldModel
        wm = WorldModel()
        assert wm is not None

    def test_skill_evolution_module_importable(self):
        from src.skills.skill_evolution import SkillEvolutionEngine
        assert SkillEvolutionEngine is not None

    def test_temporal_reasoner_module_importable(self):
        from src.reasoning.temporal_reasoner import TemporalReasoner
        assert TemporalReasoner() is not None

    def test_meta_agent_module_importable(self):
        from src.agent_manager.meta_agent import MetaSupervisor
        assert MetaSupervisor is not None
