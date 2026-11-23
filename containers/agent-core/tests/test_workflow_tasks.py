import pytest
from unittest.mock import AsyncMock, MagicMock
from src.scheduler.custom_tasks import parse_interval, fmt_interval, make_task
from src.dashboard.routes.tasks import _enrich, _fetch_all_tasks_and_goals


def test_parse_interval_workflow():
    assert parse_interval("workflow") == 0
    assert parse_interval("once") == 0
    assert parse_interval("run_once") == 0
    assert parse_interval("immediate") == 0
    assert parse_interval("") == 0
    assert parse_interval("cada 5 minutos") == 300


def test_fmt_interval_workflow():
    assert fmt_interval(0) == "Workflow Task"
    assert fmt_interval(-1) == "Workflow Task"
    assert fmt_interval(300) == "5m"


def test_make_task_workflow():
    task = make_task(
        name="Security Audit",
        instruction="Scan repository for secrets",
        interval_seconds=0,
        chat_id="12345",
    )
    assert task["task_type"] == "workflow"
    assert task["next_run"] is None
    assert task["interval_seconds"] == 0
    assert task["last_result"] == "Pending Execution"


def test_enrich_workflow_task():
    raw_task = {
        "task_id": "test1234",
        "name": "Audit",
        "instruction": "Do audit",
        "interval_seconds": 0,
        "enabled": True,
        "run_count": 0,
        "failure_count": 0,
        "last_run": None,
        "next_run": None,
        "last_result": "",
        "last_success": None,
    }
    enriched = _enrich(raw_task)
    assert enriched["is_workflow"] is True
    assert enriched["interval_fmt"] == "Workflow Task"


@pytest.mark.asyncio
async def test_fetch_all_tasks_and_goals_includes_completed():
    mock_redis = AsyncMock()
    mock_redis.hgetall = AsyncMock(return_value={})
    mock_redis.zrevrange = AsyncMock(return_value=["comp-1"])
    mock_redis.hmget = AsyncMock(return_value=['{"task_id": "comp-1", "name": "Finished Mission", "status": "completed"}'])
    mock_redis.zcard = AsyncMock(return_value=1)

    all_tasks, stats, completed = await _fetch_all_tasks_and_goals(mock_redis)
    assert stats["completed_count"] == 1
    assert len(completed) == 1
    assert completed[0]["name"] == "Finished Mission"

