"""Scheduler management routes."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

router = APIRouter()


async def _get_redis(request: Request):
    """Return a connected redis client, or None."""
    try:
        import redis.asyncio as aioredis
        return aioredis.from_url(request.app.state.redis_url, decode_responses=True)
    except Exception:
        return None


async def _list_custom_tasks(request: Request) -> list[dict]:
    r = await _get_redis(request)
    if not r:
        return []
    try:
        from ...scheduler.custom_tasks import list_tasks, fmt_interval
        tasks = await list_tasks(r)
        for t in tasks:
            t["interval_fmt"] = fmt_interval(t.get("interval_seconds") or 0)
        return tasks
    except Exception:
        return []
    finally:
        await r.aclose()

# ── Job metadata: description, category, icon path ───────────────────────────
_CAT = {
    "system":     {"label": "System",          "color": "#6366f1"},
    "memory":     {"label": "Memory",          "color": "#3b82f6"},
    "cognition":  {"label": "Cognition",       "color": "#8b5cf6"},
    "goals":      {"label": "Goals & Agents",  "color": "#f59e0b"},
    "outreach":   {"label": "Communication",   "color": "#10b981"},
}

_JOB_META: dict[str, dict] = {
    # System
    "health_check": {
        "desc": "Monitors all services and triggers self-healing alerts",
        "cat": "system",
        "icon": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4.318 6.318a4.5 4.5 0 000 6.364L12 20.364l7.682-7.682a4.5 4.5 0 00-6.364-6.364L12 7.636l-1.318-1.318a4.5 4.5 0 00-6.364 0z"/>',
    },
    "custom_task_runner": {
        "desc": "Executes user-created scheduled tasks on their defined intervals",
        "cat": "system",
        "icon": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/>',
    },
    "execution_knowledge_sync": {
        "desc": "Flushes execution knowledge from Redis to PostgreSQL for durability",
        "cat": "system",
        "icon": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/>',
    },
    "opportunities_processor": {
        "desc": "Consumes the opportunities queue and schedules proactive automations",
        "cat": "system",
        "icon": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"/>',
    },
    "execution_intelligence_monitor": {
        "desc": "Evidence-based pattern detection across execution traces",
        "cat": "system",
        "icon": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"/>',
    },
    # Memory
    "reflection": {
        "desc": "Reviews episodic memories and extracts learnings via LLM reflection",
        "cat": "memory",
        "icon": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z"/>',
    },
    "memory_cleanup": {
        "desc": "TTL expiry, importance decay, and volume caps on episodic memory",
        "cat": "memory",
        "icon": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/>',
    },
    "snapshot": {
        "desc": "Creates daily automatic memory snapshots for recovery and analysis",
        "cat": "memory",
        "icon": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 9a2 2 0 012-2h.93a2 2 0 001.664-.89l.812-1.22A2 2 0 0110.07 4h3.86a2 2 0 011.664.89l.812 1.22A2 2 0 0018.07 7H19a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V9z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 13a3 3 0 11-6 0 3 3 0 016 0z"/>',
    },
    "promotion": {
        "desc": "Elevates recurring episodic topics to semantic long-term memory",
        "cat": "memory",
        "icon": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 10l7-7m0 0l7 7m-7-7v18"/>',
    },
    "behavioral_learner": {
        "desc": "Analyzes user corrections and extracts behavioral rules for self-improvement",
        "cat": "memory",
        "icon": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253"/>',
    },
    "vector_index": {
        "desc": "Indexes memory embeddings for semantic similarity search",
        "cat": "memory",
        "icon": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 7v10c0 2.21 3.582 4 8 4s8-1.79 8-4V7M4 7c0 2.21 3.582 4 8 4s8-1.79 8-4M4 7c0-2.21 3.582-4 8-4s8 1.79 8 4"/>',
    },
    # Cognition
    "dream": {
        "desc": "Memory consolidation and LLM reflection during prolonged idle periods",
        "cat": "cognition",
        "icon": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z"/>',
    },
    "cpi_monitor": {
        "desc": "Computes Cognitive Pressure Index — governs load-shedding when agent is overloaded",
        "cat": "cognition",
        "icon": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"/>',
    },
    "self_integrity": {
        "desc": "Cross-checks self-model strengths vs actual skill rates and audit log errors",
        "cat": "cognition",
        "icon": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z"/>',
    },
    "world_model": {
        "desc": "Updates the agent's internal world model with current environmental state",
        "cat": "cognition",
        "icon": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3.055 11H5a2 2 0 012 2v1a2 2 0 002 2 2 2 0 012 2v2.945M8 3.935V5.5A2.5 2.5 0 0010.5 8h.5a2 2 0 012 2 2 2 0 104 0 2 2 0 012-2h1.064M15 20.488V18a2 2 0 012-2h3.064M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>',
    },
    "skill_evolution": {
        "desc": "Synthesizes composite skills from recurring successful execution patterns",
        "cat": "cognition",
        "icon": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/>',
    },
    "capability_evolution": {
        "desc": "Detects capability gaps after goal failures and schedules acquisition",
        "cat": "cognition",
        "icon": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 3v4M3 5h4M6 17v4m-2-2h4m5-16l2.286 6.857L21 12l-5.714 2.143L13 21l-2.286-6.857L5 12l5.714-2.143L13 3z"/>',
    },
    "capability_learner": {
        "desc": "Mines execution traces for recurring skill patterns to build capability index",
        "cat": "cognition",
        "icon": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 15l-2 5L9 9l11 4-5 2zm0 0l5 5M7.188 2.239l.777 2.897M5.136 7.965l-2.898-.777M13.95 4.05l-2.122 2.122m-5.657 5.656l-2.12 2.122"/>',
    },
    # Goals & Agents
    "goal_tick": {
        "desc": "Advances active goals one step — executes the next pending task in the graph",
        "cat": "goals",
        "icon": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 21v-4m0 0V5a2 2 0 012-2h6.5l1 1H21l-3 6 3 6h-8.5l-1-1H5a2 2 0 00-2 2zm9-13.5V9"/>',
    },
    "goal_meta_reflection": {
        "desc": "LLM analysis of completed goals to extract reusable patterns and lessons",
        "cat": "goals",
        "icon": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4"/>',
    },
    "agent_tick": {
        "desc": "Advances all active sub-agents — checks states, handles completions and failures",
        "cat": "goals",
        "icon": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"/>',
    },
    "autonomous": {
        "desc": "Evaluates system state and proactively creates goals when thresholds trigger",
        "cat": "goals",
        "icon": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/>',
    },
    "opportunity_engine": {
        "desc": "Mines conversation history to identify recurring automation opportunities",
        "cat": "goals",
        "icon": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z"/>',
    },
    # Communication / Outreach
    "reminder_checker": {
        "desc": "Checks due reminders every 30 s and delivers them via Telegram",
        "cat": "outreach",
        "icon": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9"/>',
    },
    "monitor_checker": {
        "desc": "Scans monitored URLs for changes, keywords, and new content",
        "cat": "outreach",
        "icon": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"/>',
    },
    "proactive": {
        "desc": "LLM-driven proactive messages during active hours based on context",
        "cat": "outreach",
        "icon": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"/>',
    },
    "checkin": {
        "desc": "Sends a check-in message when the user has been inactive for over an hour",
        "cat": "outreach",
        "icon": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/>',
    },
    "digest": {
        "desc": "Generates a weekly LLM narrative digest of activities and learnings",
        "cat": "outreach",
        "icon": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/>',
    },
    "subscription_checker": {
        "desc": "Checks RSS feeds and price alerts, notifies on threshold triggers",
        "cat": "outreach",
        "icon": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 5c7.18 0 13 5.82 13 13M6 11a7 7 0 017 7m-6 0a1 1 0 11-2 0 1 1 0 012 0z"/>',
    },
    "perception": {
        "desc": "Monitors crypto prices and notable events, notifies on significant changes",
        "cat": "outreach",
        "icon": '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"/>',
    },
}

_DEFAULT_ICON = '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/>'


def _fmt_interval(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        m = int(seconds / 60)
        return f"{m}m"
    elif seconds < 86400:
        h = int(seconds / 3600)
        m = int((seconds % 3600) / 60)
        return f"{h}h{f' {m}m' if m else ''}"
    d = int(seconds / 86400)
    h = int((seconds % 86400) / 3600)
    return f"{d}d{f' {h}h' if h else ''}"


def _enrich(job: dict) -> dict:
    meta = _JOB_META.get(job["name"], {})
    job["interval_fmt"] = _fmt_interval(job["interval_seconds"])
    job["desc"] = meta.get("desc", "")
    job["cat"] = meta.get("cat", "system")
    job["icon"] = meta.get("icon", _DEFAULT_ICON)
    job["cat_color"] = _CAT.get(job["cat"], _CAT["system"])["color"]
    job["cat_label"] = _CAT.get(job["cat"], _CAT["system"])["label"]
    return job


@router.get("/", response_class=HTMLResponse)
async def scheduler_page(request: Request):
    scheduler = request.app.state.scheduler
    jobs: list[dict] = []
    if scheduler:
        for job in scheduler.list_jobs():
            jobs.append(_enrich(job))

    # Group by category preserving _CAT order
    cat_order = list(_CAT.keys())
    groups: dict[str, list] = {k: [] for k in cat_order}
    for job in jobs:
        groups.setdefault(job["cat"], []).append(job)

    ordered_groups = [(cat, groups[cat]) for cat in cat_order if groups.get(cat)]
    for cat, jlist in groups.items():
        if cat not in cat_order and jlist:
            ordered_groups.append((cat, jlist))

    total   = len(jobs)
    running = sum(1 for j in jobs if not j["paused"])
    paused  = sum(1 for j in jobs if j["paused"])
    failed  = sum(1 for j in jobs if not j["last_success"] and not j["paused"])

    custom_tasks = await _list_custom_tasks(request)

    return request.app.state.templates.TemplateResponse(request, "scheduler.html", {
        "jobs": jobs,
        "groups": ordered_groups,
        "cat_meta": _CAT,
        "total": total,
        "running": running,
        "paused": paused,
        "failed": failed,
        "custom_tasks": custom_tasks,
    })


@router.get("/api/status")
async def scheduler_status(request: Request):
    """JSON endpoint for live polling."""
    scheduler = request.app.state.scheduler
    jobs = []
    if scheduler:
        for job in scheduler.list_jobs():
            jobs.append(_enrich(job))
    total   = len(jobs)
    running = sum(1 for j in jobs if not j["paused"])
    paused  = sum(1 for j in jobs if j["paused"])
    failed  = sum(1 for j in jobs if not j["last_success"] and not j["paused"])
    return JSONResponse({"jobs": jobs, "total": total, "running": running, "paused": paused, "failed": failed})


@router.post("/trigger")
async def trigger_job(request: Request):
    import urllib.parse
    form = await request.form()
    job_name = str(form.get("job", ""))
    from_overview = str(form.get("from_overview", "")) == "1"
    ajax = request.headers.get("x-requested-with") == "xmlhttprequest"

    result = ""
    if job_name and request.app.state.scheduler:
        # Manual trigger of Proactive bypasses quiet-hours and daily-limit
        if job_name == "proactive":
            try:
                import redis.asyncio as aioredis
                _r = aioredis.from_url(request.app.state.redis_url, decode_responses=True)
                await _r.set("proactive:manual_force", "1", ex=60)
                await _r.aclose()
            except Exception:
                pass
        result = await request.app.state.scheduler.trigger(job_name)

    if ajax:
        ok = not result.startswith("Error")
        return JSONResponse({"ok": ok, "result": result})

    if from_overview or job_name == "digest":
        ok_flag = "0" if result.startswith("Error") else "1"
        msg = urllib.parse.quote(result[:120], safe="")
        return RedirectResponse(
            f"/overview?triggered={job_name}&ok={ok_flag}&msg={msg}",
            status_code=303,
        )
    return RedirectResponse("/scheduler", status_code=303)


@router.post("/pause")
async def pause_job(request: Request):
    form = await request.form()
    job_name = str(form.get("job", ""))
    ajax = request.headers.get("x-requested-with") == "xmlhttprequest"
    if job_name and request.app.state.scheduler:
        await request.app.state.scheduler.pause(job_name)
    if ajax:
        return JSONResponse({"ok": True})
    return RedirectResponse("/scheduler", status_code=303)


@router.post("/resume")
async def resume_job(request: Request):
    form = await request.form()
    job_name = str(form.get("job", ""))
    ajax = request.headers.get("x-requested-with") == "xmlhttprequest"
    if job_name and request.app.state.scheduler:
        await request.app.state.scheduler.resume(job_name)
    if ajax:
        return JSONResponse({"ok": True})
    return RedirectResponse("/scheduler", status_code=303)


# ── Custom Tasks API ──────────────────────────────────────────────────────────

@router.get("/api/custom-tasks")
async def custom_tasks_list(request: Request):
    tasks = await _list_custom_tasks(request)
    return JSONResponse({"tasks": tasks})


@router.post("/custom-tasks/create")
async def custom_task_create(request: Request):
    form = await request.form()
    name     = str(form.get("name", "")).strip()
    instr    = str(form.get("instruction", "")).strip()
    interval = str(form.get("interval", "")).strip()

    if not name or not instr or not interval:
        return JSONResponse({"ok": False, "error": "name, instruction and interval are required"}, status_code=400)

    r = await _get_redis(request)
    if not r:
        return JSONResponse({"ok": False, "error": "redis unavailable"}, status_code=503)
    try:
        from ...scheduler.custom_tasks import parse_interval, make_task, save_task
        secs = parse_interval(interval)
        if not secs or secs < 60:
            return JSONResponse({"ok": False, "error": "Minimum interval is 60 seconds"}, status_code=400)
        task = make_task(name=name, instruction=instr, interval_seconds=secs)
        await save_task(r, task)
        return JSONResponse({"ok": True, "task_id": task["task_id"]})
    finally:
        await r.aclose()


@router.post("/custom-tasks/toggle")
async def custom_task_toggle(request: Request):
    form = await request.form()
    task_id = str(form.get("task_id", ""))
    if not task_id:
        return JSONResponse({"ok": False, "error": "missing task_id"}, status_code=400)
    r = await _get_redis(request)
    if not r:
        return JSONResponse({"ok": False, "error": "redis unavailable"}, status_code=503)
    try:
        from ...scheduler.custom_tasks import get_task, save_task
        task = await get_task(r, task_id)
        if not task:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        task["enabled"] = not task.get("enabled", True)
        await save_task(r, task)
        return JSONResponse({"ok": True, "enabled": task["enabled"]})
    finally:
        await r.aclose()


@router.post("/custom-tasks/delete")
async def custom_task_delete(request: Request):
    form = await request.form()
    task_id = str(form.get("task_id", ""))
    if not task_id:
        return JSONResponse({"ok": False, "error": "missing task_id"}, status_code=400)
    r = await _get_redis(request)
    if not r:
        return JSONResponse({"ok": False, "error": "redis unavailable"}, status_code=503)
    try:
        from ...scheduler.custom_tasks import delete_task
        deleted = await delete_task(r, task_id)
        return JSONResponse({"ok": deleted})
    finally:
        await r.aclose()
