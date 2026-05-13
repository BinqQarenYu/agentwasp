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
    task["interval_fmt"] = fmt_interval(task["interval_seconds"])
    task["destinations"] = _detect_destinations(task)
    return task


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
        tasks = await list_tasks(r)
    finally:
        await r.aclose()
    for t in tasks:
        _enrich(t)
    return JSONResponse({**_compute_stats(tasks), "tasks": tasks})


@router.get("/", response_class=HTMLResponse)
async def tasks_page(request: Request):
    r = _get_redis(request)
    try:
        tasks = await list_tasks(r)
        for t in tasks:
            _enrich(t)
    finally:
        await r.aclose()

    return request.app.state.templates.TemplateResponse(request, "tasks.html", {
        "tasks": tasks,
        "stats": _compute_stats(tasks),
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
    if not interval_text:
        return JSONResponse({"ok": False, "error": "Interval is required"})

    seconds = parse_interval(interval_text)
    if not seconds or seconds < 60:
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
