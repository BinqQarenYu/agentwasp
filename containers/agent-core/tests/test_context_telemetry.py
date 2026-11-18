import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.agent.context import SYSTEM_PROMPT, IDENTITY_FEWSHOT, build_context
from src.models.types import Message


def test_system_prompt_antigravity_knowledge():
    """Verify SYSTEM_PROMPT contains the full 16-agent workforce, capabilities matrix, and codebase map."""
    # 1. 16 Digital Employees
    expected_employees = [
        "Prometheus", "Hermes", "Clara", "Calliope", "Ledger",
        "Sherlock", "Argus", "Odyssey", "Echo", "Dr. Albert",
        "Apollo", "Hunter", "Midas", "Aegis", "Mercury", "Athena", "Mue",
    ]
    for emp in expected_employees:
        assert emp in SYSTEM_PROMPT, f"Missing employee {emp} in SYSTEM_PROMPT"

    # 2. Antigravity Capabilities & Invariants
    assert "ANTIGRAVITY CAPABILITIES SPECTRUM" in SYSTEM_PROMPT
    assert "WHAT YOU CAN DO" in SYSTEM_PROMPT
    assert "WHAT YOU CANNOT DO" in SYSTEM_PROMPT
    assert "read_file" in SYSTEM_PROMPT
    assert "edit_file" in SYSTEM_PROMPT
    assert "list_dir" in SYSTEM_PROMPT
    assert "search_files" in SYSTEM_PROMPT
    assert "Approval Cards" in SYSTEM_PROMPT

    # 3. Codebase Architecture Map
    assert "CODEBASE ARCHITECTURE & SOVEREIGN REPOSITORY MAP" in SYSTEM_PROMPT
    assert "/app/src/" in SYSTEM_PROMPT
    assert "agent_manager" in SYSTEM_PROMPT
    assert "goal_orchestrator" in SYSTEM_PROMPT
    assert "harnesses" in SYSTEM_PROMPT
    assert "waspctl" in SYSTEM_PROMPT


def test_identity_fewshots_coverage():
    """Verify few-shots cover employees, capabilities, codebase, and active state."""
    questions = [q for q, _ in IDENTITY_FEWSHOT]
    assert any("employees" in q.lower() or "who works with you" in q.lower() for q in questions)
    assert any("capabilities" in q.lower() or "what can you do" in q.lower() for q in questions)
    assert any("codebase" in q.lower() or "where is your code" in q.lower() for q in questions)
    assert any("currently doing" in q.lower() or "what's running" in q.lower() for q in questions)


@pytest.mark.asyncio
async def test_build_context_injects_live_telemetry():
    """Verify build_context injects live workforce & execution state telemetry."""
    mock_session = AsyncMock()
    mock_memory = MagicMock()
    mock_memory.retrieve = AsyncMock(return_value=[])

    with patch("redis.asyncio.from_url") as mock_redis_from_url:
        mock_r = AsyncMock()
        # Mock active goal in Redis
        mock_r.hgetall.side_effect = lambda key: {
            "agents": {
                "root-liaison": '{"id":"root-liaison","name":"Prometheus","status":"running","metadata":{"mesh_role":"Chief of Staff"}}',
                "bot-16": '{"id":"bot-16","name":"Mue","status":"running","metadata":{"mesh_role":"System Homeostasis"}}',
            },
            "goals": {
                "goal-1": '{"id":"goal-1","title":"Test Active Goal","state":"in_progress","progress":50.0,"task_graph":{"nodes":[{"id":"t1","status":"in_progress","description":"Executing unit tests"}]}}',
            },
            "custom_tasks": {
                "task-1": '{"id":"task-1","name":"Crypto Hourly Scanner","interval":"hourly","next_run":"2026-11-19T00:00:00Z","enabled":true}',
            },
        }.get(key, {})

        mock_redis_from_url.return_value = mock_r

        messages = await build_context(
            session=mock_session,
            memory=mock_memory,
            user_text="What are you currently doing?",
            chat_id="test-chat",
            model_name="gemini-2.5-flash",
            provider_name="google",
            redis_url="redis://localhost:6379/0",
        )

        assert len(messages) >= 1
        sys_content = messages[0].content

        # Verify live execution state injected
        assert "[CURRENT SYSTEM EXECUTION STATE & REAL-TIME TELEMETRY (LIVE SELF-AWARENESS)]" in sys_content
        assert "Test Active Goal" in sys_content
        assert "Crypto Hourly Scanner" in sys_content
        assert "Prometheus" in sys_content
        assert "Mue" in sys_content
