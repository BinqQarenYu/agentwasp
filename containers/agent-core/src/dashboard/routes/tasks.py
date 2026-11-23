"""Custom scheduled tasks — dashboard routes."""

from __future__ import annotations

import re
from datetime import datetime, timezone

import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ...scheduler.custom_tasks import (
    delete_task, fmt_interval, get_task,
    list_tasks, make_task, next_run_from_now, parse_interval, save_task,
)

router = APIRouter()
logger = structlog.get_logger()


def _get_redis(request: Request):
    return aioredis.from_url(request.app.state.redis_url, decode_responses=True)


def _detect_destinations(task: dict) -> list[str]:
    """Detect delivery destinations from task metadata.

    Uses structured fields first (chat_id → Telegram), then keyword-scans
    the instruction for email and Slack references.
    """
    dests: list[str] = []
    if task.get("chat_id"):
        dests.append("telegram")
    instr = (task.get("instruction") or "").lower()
    if re.search(r"\b(email|gmail|correo|mail|e-mail|por\s+correo)\b", instr):
        dests.append("email")
    if re.search(r"\bslack\b", instr):
        dests.append("slack")
    return dests


def _enrich(task: dict) -> dict:
    sec = task.get("interval_seconds", 0)
    if sec <= 0:
        task["interval_fmt"] = "Workflow Task"
        task["is_workflow"] = True
    else:
        task["interval_fmt"] = fmt_interval(sec)
        task["is_workflow"] = False
    task["destinations"] = _detect_destinations(task)
    return task


async def _fetch_all_tasks_and_goals(r) -> tuple[list[dict], dict, list[dict]]:
    tasks = await list_tasks(r)
    for t in tasks:
        _enrich(t)

    # Also load goals from goal_orchestrator
    goals_as_tasks = []
    try:
        from ...goal_orchestrator.store import list_goals
        goals = await list_goals(r)
        for g in goals:
            completed_nodes = sum(1 for n in (g.task_graph.nodes if g.task_graph else []) if n.status.value == "done")
            total_nodes = len(g.task_graph.nodes) if g.task_graph else 0
            goals_as_tasks.append({
                "task_id": g.id,
                "name": g.title or g.objective[:50],
                "instruction": g.objective,
                "interval_seconds": 0,
                "interval_fmt": f"Goal ({g.state.value.upper()})",
                "next_run": None,
                "last_run": g.started_at or g.created_at,
                "last_result": f"Progress: {g.progress}% ({completed_nodes}/{total_nodes} steps done)" if total_nodes else f"State: {g.state.value.upper()}",
                "last_success": True if g.state.value == "completed" else (False if g.state.value == "failed" else None),
                "run_count": g.step_count,
                "failure_count": 1 if g.state.value == "failed" else 0,
                "enabled": g.state.value in ("active", "planning"),
                "is_workflow": True,
                "is_goal": True,
                "destinations": [],
                "created_at": g.created_at or "",
            })
    except Exception as e:
        logger.debug("tasks.list_goals_skip", error=str(e)[:80])

    # Load completed tasks archive
    completed_tasks = []
    completed_count = 0
    try:
        from ...scheduler.completed_tasks import list_completed_tasks, get_completed_stats
        completed_tasks = await list_completed_tasks(r, limit=50)
        c_stats = await get_completed_stats(r)
        completed_count = c_stats.get("total_completed", len(completed_tasks))
    except Exception as e:
        logger.debug("tasks.list_completed_skip", error=str(e)[:80])

    all_tasks = tasks + goals_as_tasks
    all_tasks.sort(key=lambda t: t.get("created_at") or "", reverse=True)

    stats = {
        "total": len(all_tasks),
        "active": sum(1 for t in all_tasks if t.get("enabled", True) and t.get("last_success") is not False),
        "workflow_count": sum(1 for t in all_tasks if t.get("is_workflow", False)),
        "scheduled_count": sum(1 for t in all_tasks if not t.get("is_workflow", False)),
        "paused": sum(1 for t in all_tasks if not t.get("enabled", True)),
        "failed": sum(1 for t in all_tasks if t.get("last_success") is False),
        "completed_count": completed_count,
    }
    return all_tasks, stats, completed_tasks


def _compute_stats(tasks: list[dict]) -> dict:
    total = len(tasks)
    paused = sum(1 for t in tasks if not t.get("enabled", True))
    failed = sum(1 for t in tasks if t.get("last_success") is False and t.get("enabled", True))
    active = total - paused - failed
    return {"total": total, "active": max(active, 0), "paused": paused, "failed": failed}


@router.get("/api/status")
async def tasks_status(request: Request):
    r = _get_redis(request)
    try:
        all_tasks, stats, completed_tasks = await _fetch_all_tasks_and_goals(r)
    finally:
        await r.aclose()
    return JSONResponse({**stats, "tasks": all_tasks, "completed_tasks": completed_tasks})


@router.get("/api/completed")
async def tasks_completed(request: Request):
    r = _get_redis(request)
    try:
        from ...scheduler.completed_tasks import list_completed_tasks, get_completed_stats
        completed = await list_completed_tasks(r, limit=100)
        stats = await get_completed_stats(r)
    finally:
        await r.aclose()
    return JSONResponse({"ok": True, "total": stats.get("total_completed", len(completed)), "tasks": completed})


@router.get("/", response_class=HTMLResponse)
async def tasks_page(request: Request):
    r = _get_redis(request)
    try:
        all_tasks, stats, completed_tasks = await _fetch_all_tasks_and_goals(r)
    finally:
        await r.aclose()

    return request.app.state.templates.TemplateResponse(request, "tasks.html", {
        "tasks": all_tasks,
        "stats": stats,
        "completed_tasks": completed_tasks,
    })


@router.post("/create")
async def create_task(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    name = (body.get("name") or "").strip()
    instruction = (body.get("instruction") or "").strip()
    interval_text = (body.get("interval") or "").strip()

    if not name:
        return JSONResponse({"ok": False, "error": "Name is required"})
    if not instruction:
        return JSONResponse({"ok": False, "error": "Instruction is required"})

    if not interval_text or interval_text.lower() in ("workflow", "once", "run_once", "now", "una vez"):
        seconds = 0
    else:
        seconds = parse_interval(interval_text)
        if seconds is None or seconds < 0 or (0 < seconds < 60):
            return JSONResponse({"ok": False, "error": f"Could not parse interval '{interval_text}'"})

    from ...config import settings
    chat_id = settings.scheduler_notify_chat_id

    task = make_task(name=name, instruction=instruction, interval_seconds=seconds, chat_id=chat_id)

    r = _get_redis(request)
    try:
        await save_task(r, task)
    finally:
        await r.aclose()

    logger.info("tasks.created", name=name, interval=seconds)
    return JSONResponse({"ok": True, "task_id": task["task_id"], "name": name})


@router.post("/delete")
async def delete_task_route(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    task_id = (body.get("task_id") or "").strip()
    if not task_id:
        return JSONResponse({"ok": False, "error": "task_id is required"})

    r = _get_redis(request)
    try:
        ok = await delete_task(r, task_id)
    finally:
        await r.aclose()

    if ok:
        logger.info("tasks.deleted", task_id=task_id)
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False, "error": "Task not found"})


@router.post("/toggle")
async def toggle_task(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    task_id = (body.get("task_id") or "").strip()
    if not task_id:
        return JSONResponse({"ok": False, "error": "task_id is required"})

    r = _get_redis(request)
    try:
        task = await get_task(r, task_id)
        if not task:
            return JSONResponse({"ok": False, "error": "Task not found"})
        task["enabled"] = not task.get("enabled", True)
        await save_task(r, task)
        state = "enabled" if task["enabled"] else "disabled"
    finally:
        await r.aclose()

    return JSONResponse({"ok": True, "enabled": task["enabled"], "state": state})


@router.post("/update")
async def update_task(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    task_id = (body.get("task_id") or "").strip()
    if not task_id:
        return JSONResponse({"ok": False, "error": "task_id is required"})

    r = _get_redis(request)
    try:
        task = await get_task(r, task_id)
        if not task:
            return JSONResponse({"ok": False, "error": "Task not found"})

        changes = []
        new_name = (body.get("name") or "").strip()
        new_instruction = (body.get("instruction") or "").strip()
        new_interval = (body.get("interval") or "").strip()

        if new_name and new_name != task.get("name"):
            task["name"] = new_name
            changes.append("name")
        if new_instruction and new_instruction != task.get("instruction"):
            task["instruction"] = new_instruction
            changes.append("instruction")
        if new_interval:
            seconds = parse_interval(new_interval)
            if not seconds or seconds < 60:
                return JSONResponse({"ok": False, "error": f"Could not parse interval '{new_interval}'"})
            if seconds != task.get("interval_seconds"):
                task["interval_seconds"] = seconds
                task["next_run"] = next_run_from_now(seconds)
                changes.append("interval")

        if changes:
            await save_task(r, task)
            logger.info("tasks.updated", task_id=task_id, changes=changes)
    finally:
        await r.aclose()

    return JSONResponse({"ok": True, "changed": changes, "name": task.get("name", task_id)})


@router.post("/trigger")
async def trigger_task(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    task_id = (body.get("task_id") or "").strip()
    if not task_id:
        return JSONResponse({"ok": False, "error": "task_id is required"})

    r = _get_redis(request)
    try:
        task = await get_task(r, task_id)
        if not task:
            return JSONResponse({"ok": False, "error": "Task not found"})
        # Set next_run to now so CustomTaskRunnerJob picks it up in next cycle (<60s)
        task["next_run"] = datetime.now(timezone.utc).isoformat()
        await save_task(r, task)
        name = task.get("name", task_id)
    finally:
        await r.aclose()

    logger.info("tasks.triggered_manually", task_id=task_id)
    return JSONResponse({"ok": True, "message": f"Task '{name}' will run in the next scheduler cycle (<60s)"})
