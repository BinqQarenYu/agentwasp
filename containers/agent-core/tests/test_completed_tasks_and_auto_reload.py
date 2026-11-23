"""Tests for failure root-cause diagnosis, auto-reload loop, and completed tasks database."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.db.models import CompletedTaskRecord
from src.events.handlers import EventHandler
from src.scheduler.completed_tasks import (
    REDIS_KEY_COMPLETED,
    REDIS_KEY_COMPLETED_INDEX,
    archive_completed_task,
    get_completed_stats,
    get_completed_task,
    list_completed_tasks,
)
from src.scheduler.custom_tasks import make_task


class FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.zsets = {}

    async def hset(self, key, field, value):
        if key not in self.hashes:
            self.hashes[key] = {}
        self.hashes[key][field] = value
        return 1

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    async def hgetall(self, key):
        return self.hashes.get(key, {})

    async def hmget(self, key, fields):
        h = self.hashes.get(key, {})
        return [h.get(f) for f in fields]

    async def hdel(self, key, field):
        if key in self.hashes and field in self.hashes[key]:
            del self.hashes[key][field]
            return 1
        return 0

    async def zadd(self, key, mapping):
        if key not in self.zsets:
            self.zsets[key] = {}
        for member, score in mapping.items():
            self.zsets[key][member] = float(score)
        return len(mapping)

    async def zrevrange(self, key, start, stop):
        z = self.zsets.get(key, {})
        # Sort by score descending
        sorted_members = [m for m, _ in sorted(z.items(), key=lambda x: x[1], reverse=True)]
        if stop < 0:
            return sorted_members[start:]
        return sorted_members[start:stop + 1]

    async def zcard(self, key):
        return len(self.zsets.get(key, {}))

    async def zrem(self, key, member):
        if key in self.zsets and member in self.zsets[key]:
            del self.zsets[key][member]
            return 1
        return 0

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_archive_completed_task_dual_persists():
    r = FakeRedis()
    task = make_task(
        name="Scrape Competitor Pricing",
        instruction="Extract product prices from catalog",
        interval_seconds=0,
        task_id="task-complete-1",
    )
    task["auto_reload_count"] = 1
    task["last_error_diagnosis"] = "TOOL_FAILURE (browser): Navigation timeout"

    mock_session = AsyncMock()
    mock_session.add = MagicMock()

    archive = await archive_completed_task(
        r,
        task=task,
        final_output="Extracted 42 items successfully",
        trace_spans=[{"skill": "browser", "success": True}],
        session=mock_session,
    )

    assert archive["task_id"] == "task-complete-1"
    assert archive["name"] == "Scrape Competitor Pricing"
    assert archive["status"] == "completed"
    assert "Self-healed" in archive["diagnostic_notes"]
    assert archive["retry_count"] == 1
    assert archive["final_output"] == "Extracted 42 items successfully"

    # Verify Redis presence
    raw_saved = await r.hget(REDIS_KEY_COMPLETED, "task-complete-1")
    assert raw_saved is not None
    saved_dict = json.loads(raw_saved)
    assert saved_dict["task_id"] == "task-complete-1"

    z_count = await r.zcard(REDIS_KEY_COMPLETED_INDEX)
    assert z_count == 1

    # Verify Postgres session received CompletedTaskRecord
    assert mock_session.add.called
    added_obj = mock_session.add.call_args[0][0]
    assert isinstance(added_obj, CompletedTaskRecord)
    assert added_obj.task_id == "task-complete-1"
    assert added_obj.status == "completed"
    assert mock_session.commit.called


@pytest.mark.asyncio
async def test_list_and_get_completed_tasks():
    r = FakeRedis()
    task1 = make_task("Task 1", "Do something", 0, task_id="t-1")
    task2 = make_task("Task 2", "Do another thing", 0, task_id="t-2")

    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    await archive_completed_task(r, task1, "Output 1", session=mock_session)
    await archive_completed_task(r, task2, "Output 2", session=mock_session)

    stats = await get_completed_stats(r)
    assert stats["total_completed"] == 2

    completed_list = await list_completed_tasks(r, limit=10)
    assert len(completed_list) == 2

    single = await get_completed_task(r, "t-1")
    assert single is not None
    assert single["name"] == "Task 1"


def test_diagnose_task_failure_scenarios():
    task = {"instruction": "Check server health"}

    # 1. Timeout
    diag_timeout = EventHandler._diagnose_task_failure(
        outcome="timeout",
        response_text="timeout",
        trace_spans=[],
        task=task,
    )
    assert "TIMEOUT:" in diag_timeout

    # 2. Exception
    diag_ex = EventHandler._diagnose_task_failure(
        outcome="exception",
        response_text="Connection refused to docker daemon",
        trace_spans=[],
        task=task,
    )
    assert "EXCEPTION:" in diag_ex
    assert "Connection refused" in diag_ex

    # 3. Tool Failure
    spans_fail = [
        {"skill": "browser", "success": False, "error": "Target page crashed with status 502"}
    ]
    diag_tool = EventHandler._diagnose_task_failure(
        outcome="completed",
        response_text="Error occurred",
        trace_spans=spans_fail,
        task=task,
    )
    assert "TOOL_FAILURE (browser):" in diag_tool
    assert "502" in diag_tool

    # 4. Required action missing (Email)
    email_task = {"instruction": "Send email to team@company.com with report"}
    diag_missing_email = EventHandler._diagnose_task_failure(
        outcome="completed",
        response_text="Everything looks fine",
        trace_spans=[{"skill": "shell", "success": True}],
        task=email_task,
    )
    assert "MISSING_ACTION:" in diag_missing_email
    assert "Gmail" in diag_missing_email

    # 5. Failsafe phrase
    diag_failsafe = EventHandler._diagnose_task_failure(
        outcome="completed",
        response_text="Lo siento, tardó demasiado tiempo en procesar",
        trace_spans=[{"skill": "shell", "success": True}],
        task=task,
    )
    assert "FAILSAFE_TRIGGERED:" in diag_failsafe


@pytest.mark.asyncio
async def test_writeback_auto_reload_loop():
    r = FakeRedis()
    task = make_task(
        name="API Data Sync",
        instruction="Sync database records",
        interval_seconds=0,
        task_id="sync-task-1",
    )
    task["enabled"] = False  # Set to false by scheduler when launched
    await r.hset("custom_tasks", "sync-task-1", json.dumps(task))

    handler = MagicMock()
    handler.redis_url = "redis://mock"
    handler._diagnose_task_failure = EventHandler._diagnose_task_failure

    with patch("redis.asyncio.from_url", return_value=r):
        # First failure: should diagnose and schedule reload 1
        await EventHandler._writeback_task_outcome(
            handler,
            task_id="sync-task-1",
            task_name="API Data Sync",
            response_text="No pude generar una respuesta",
            trace_spans=[],
            outcome="completed",
        )

        task_after_1 = json.loads(await r.hget("custom_tasks", "sync-task-1"))
        assert task_after_1["last_success"] is False
        assert task_after_1["auto_reload_count"] == 1
        assert task_after_1["enabled"] is True
        assert task_after_1["next_run"] is not None
        assert "Auto-Reloading (Attempt 1/3" in task_after_1["last_result"]
        assert "self_healing_context" in task_after_1
        assert len(task_after_1["failure_history"]) == 1

        # Second failure: should schedule reload 2
        await EventHandler._writeback_task_outcome(
            handler,
            task_id="sync-task-1",
            task_name="API Data Sync",
            response_text="No pude generar una respuesta",
            trace_spans=[],
            outcome="completed",
        )
        task_after_2 = json.loads(await r.hget("custom_tasks", "sync-task-1"))
        assert task_after_2["auto_reload_count"] == 2
        assert "Auto-Reloading (Attempt 2/3" in task_after_2["last_result"]

        # Now success: completes and self-heals!
        with patch("src.scheduler.completed_tasks.archive_completed_task", new_callable=AsyncMock) as mock_archive:
            await EventHandler._writeback_task_outcome(
                handler,
                task_id="sync-task-1",
                task_name="API Data Sync",
                response_text="Sync finished successfully with 150 rows.",
                trace_spans=[{"skill": "python_exec", "success": True}],
                outcome="completed",
            )
            assert mock_archive.called
            archived_task = mock_archive.call_args[1]["task"]
            assert archived_task["last_success"] is True
            assert archived_task["auto_reload_count"] == 0
