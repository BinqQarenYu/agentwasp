"""Completed Tasks Archive — persistent database and Redis cache for finished tasks.

Provides a permanent database of completed workflow and scheduled tasks,
ensuring zero amnesia and complete auditability of deliverables, runtimes,
and self-healing diagnostic notes.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from uuid import uuid4

import structlog

logger = structlog.get_logger()

REDIS_KEY_COMPLETED = "completed_tasks"
REDIS_KEY_COMPLETED_INDEX = "completed_tasks:index"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _calc_duration_seconds(task: dict, completed_at: datetime) -> float:
    """Calculate task execution duration in seconds from created_at or last_run."""
    start_str = task.get("last_run") or task.get("created_at")
    if not start_str:
        return 0.0
    try:
        start_dt = datetime.fromisoformat(start_str)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        diff = (completed_at - start_dt).total_seconds()
        return max(0.0, round(diff, 2))
    except Exception:
        return 0.0


def _build_diagnostic_resolution_notes(task: dict) -> str:
    """Build summary notes if the task previously failed and was self-healed."""
    fail_count = int(task.get("failure_count", 0) or 0)
    reload_count = int(task.get("auto_reload_count", 0) or 0)
    last_diag = task.get("last_error_diagnosis", "")
    history = task.get("failure_history", [])

    if reload_count > 0 or fail_count > 0 or last_diag:
        attempts_str = f"Self-healed after {reload_count or fail_count} retry attempt(s)."
        if last_diag:
            return f"{attempts_str} Overcame previous issue: {last_diag}"
        if history and isinstance(history, list) and len(history) > 0:
            latest = history[-1]
            diag = latest.get("diagnosis") or latest.get("reason") or ""
            return f"{attempts_str} Overcame previous issue: {diag}"
        return attempts_str
    return "Completed on first attempt with no execution errors."


async def archive_completed_task(
    r,
    task: dict,
    final_output: str = "",
    trace_spans: list[dict] | None = None,
    session=None,
) -> dict:
    """Archive a completed task to Redis and PostgreSQL.

    1. Computes runtime metrics, diagnostic resolution notes, and result summaries.
    2. Writes to Redis HASH 'completed_tasks' and ZSET 'completed_tasks:index'.
    3. Persists to PostgreSQL 'completed_tasks' table.
    4. If the task is a workflow task (interval_seconds <= 0), removes it from
       active 'custom_tasks' so it cleanly transitions into the completed archive.
    """
    now = _now_utc()
    completed_at_iso = now.isoformat()
    completed_ts = now.timestamp()
    task_id = str(task.get("task_id") or uuid4())
    record_id = str(uuid4())

    duration_sec = _calc_duration_seconds(task, now)
    diag_notes = _build_diagnostic_resolution_notes(task)
    retries = int(task.get("auto_reload_count", 0) or 0)

    # Clean truncated summary for card display
    clean_summary = (final_output or "").strip()
    if len(clean_summary) > 200:
        summary_preview = clean_summary[:197] + "..."
    else:
        summary_preview = clean_summary or "Completed successfully."

    task_type = task.get("task_type") or ("workflow" if task.get("interval_seconds", 0) <= 0 else "scheduled")

    archive_entry = {
        "id": record_id,
        "task_id": task_id,
        "name": task.get("name", "Untitled Task"),
        "instruction": task.get("instruction", ""),
        "task_type": task_type,
        "status": "completed",
        "result_summary": summary_preview,
        "final_output": final_output,
        "diagnostic_notes": diag_notes,
        "retry_count": retries,
        "duration_seconds": duration_sec,
        "chat_id": str(task.get("chat_id", "")),
        "created_at": task.get("created_at", completed_at_iso),
        "completed_at": completed_at_iso,
        "metadata": {
            "interval_seconds": task.get("interval_seconds", 0),
            "total_runs": task.get("run_count", 1),
            "failure_history": task.get("failure_history", []),
            "span_count": len(trace_spans or []),
        },
    }

    # 1. Dual-persist to Redis
    try:
        await r.hset(REDIS_KEY_COMPLETED, task_id, json.dumps(archive_entry, default=str))
        await r.zadd(REDIS_KEY_COMPLETED_INDEX, {task_id: completed_ts})
    except Exception as re_err:
        logger.warning("completed_tasks.redis_persist_error", error=str(re_err)[:120], task_id=task_id)

    # 2. Persist to PostgreSQL (non-blocking / error-resilient)
    try:
        from ..db.models import CompletedTaskRecord
        from ..db.session import async_session

        async def _save_pg(s):
            db_record = CompletedTaskRecord(
                id=record_id,
                task_id=task_id,
                name=archive_entry["name"],
                instruction=archive_entry["instruction"],
                task_type=archive_entry["task_type"],
                status=archive_entry["status"],
                result_summary=archive_entry["result_summary"],
                final_output=archive_entry["final_output"],
                diagnostic_notes=archive_entry["diagnostic_notes"],
                retry_count=archive_entry["retry_count"],
                duration_seconds=archive_entry["duration_seconds"],
                chat_id=archive_entry["chat_id"],
                created_at=datetime.fromisoformat(archive_entry["created_at"]),
                completed_at=now,
                metadata_json=archive_entry["metadata"],
            )
            s.add(db_record)
            await s.commit()

        if session:
            await _save_pg(session)
        else:
            async with async_session() as s:
                await _save_pg(s)

        logger.info(
            "completed_tasks.archived",
            task_id=task_id,
            name=archive_entry["name"],
            retries=retries,
            duration=duration_sec,
        )
    except Exception as pg_err:
        logger.warning("completed_tasks.pg_persist_error", error=str(pg_err)[:120], task_id=task_id)

    # 3. Clean transition for one-shot workflow tasks
    if task.get("interval_seconds", 0) <= 0:
        try:
            from .custom_tasks import REDIS_KEY as CUSTOM_TASKS_KEY
            # Mark custom task completed/archived or remove from active queue
            await r.hdel(CUSTOM_TASKS_KEY, task_id)
            logger.info("completed_tasks.workflow_promoted_from_active", task_id=task_id)
        except Exception as cl_err:
            logger.debug("completed_tasks.cleanup_custom_tasks_skip", error=str(cl_err)[:80])

    return archive_entry


async def list_completed_tasks(r, limit: int = 50, offset: int = 0) -> list[dict]:
    """Retrieve chronologically ordered completed tasks (most recent first)."""
    try:
        # zrevrange returns highest scores (newest) first
        task_ids = await r.zrevrange(REDIS_KEY_COMPLETED_INDEX, offset, offset + limit - 1)
        if not task_ids:
            return []

        raw_entries = await r.hmget(REDIS_KEY_COMPLETED, task_ids)
        items = []
        for raw in raw_entries:
            if raw:
                try:
                    items.append(json.loads(raw))
                except Exception:
                    pass
        return items
    except Exception as e:
        logger.warning("completed_tasks.list_error", error=str(e)[:120])
        return []


async def get_completed_task(r, task_id: str) -> dict | None:
    """Retrieve a single completed task record by task_id."""
    try:
        raw = await r.hget(REDIS_KEY_COMPLETED, task_id)
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return None


async def get_completed_stats(r) -> dict:
    """Return metrics on completed tasks archive."""
    try:
        count = await r.zcard(REDIS_KEY_COMPLETED_INDEX)
        return {"total_completed": count}
    except Exception:
        return {"total_completed": 0}


async def delete_completed_task(r, task_id: str) -> bool:
    """Remove a completed task from Redis archive."""
    try:
        await r.hdel(REDIS_KEY_COMPLETED, task_id)
        await r.zrem(REDIS_KEY_COMPLETED_INDEX, task_id)
        return True
    except Exception:
        return False
