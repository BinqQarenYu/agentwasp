"""Overview dashboard route."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import cast, Date, func, select

from ...db.models import AuditLog
from ...db.session import async_session

router = APIRouter()


@router.get("/overview", response_class=HTMLResponse)
async def overview(request: Request):
    memory = request.app.state.memory
    mm = request.app.state.model_manager
    sr = request.app.state.skill_registry
    scheduler = request.app.state.scheduler

    stats = memory.get_stats()
    model_status = mm.get_status()

    skills_count = 0
    skills_enabled = 0
    if sr:
        skills_count = len(sr.list_all())
        skills_enabled = len(sr.list_enabled())

    scheduler_jobs = scheduler.list_jobs() if scheduler else []

    snapshots = memory.list_snapshots()

    from ...config import now_local, settings
    tz = settings.timezone
    now_str = now_local().strftime("%a %d %b %Y · %H:%M")

    # Performance summary (optional — won't break page if unavailable)
    perf_summary = None
    econ_today = None
    try:
        introspector = request.app.state.introspector
        if introspector:
            perf_summary = await introspector.get_performance(hours=24)
    except Exception:
        pass
    try:
        from ...observability.economics import economics as _ec
        econ = _ec.get_summary()
        econ_today = econ.get("today", {})
    except Exception:
        pass

    # Activity heatmap: last 30 days from audit_log
    heatmap = []
    streak = 0
    try:
        today = datetime.now(timezone.utc).date()
        thirty_days_ago = today - timedelta(days=29)
        cutoff = datetime(thirty_days_ago.year, thirty_days_ago.month, thirty_days_ago.day, tzinfo=timezone.utc)

        async with async_session() as session:
            rows = await session.execute(
                select(
                    cast(AuditLog.timestamp, Date).label("day"),
                    func.count(AuditLog.id).label("cnt"),
                )
                .where(AuditLog.timestamp >= cutoff)
                .group_by("day")
                .order_by("day")
            )
            day_counts = {str(row.day): row.cnt for row in rows}

        # Build 30-day list (oldest → newest)
        for i in range(30):
            d = thirty_days_ago + timedelta(days=i)
            cnt = day_counts.get(str(d), 0)
            heatmap.append({"date": str(d), "count": cnt})

        # Consecutive-day streak (from today backwards)
        check = today
        while True:
            if day_counts.get(str(check), 0) > 0:
                streak += 1
                check -= timedelta(days=1)
            else:
                break
            if streak > 30:
                break
    except Exception:
        heatmap = [{"date": "", "count": 0}] * 30

    # Wasp Digest — load from Redis; auto-trigger generation if missing or >24h old
    digest = None
    try:
        import json
        import redis.asyncio as aioredis
        from datetime import timezone as _tz
        r = aioredis.from_url(request.app.state.redis_url, decode_responses=True)
        raw = await r.get("agent:digest")
        await r.aclose()
        if raw:
            digest = json.loads(raw)
        # Auto-trigger background refresh if missing or stale (>24h)
        stale = True
        if digest:
            try:
                last = datetime.fromisoformat(digest["generated_at"])
                stale = (datetime.now(_tz.utc) - last).total_seconds() > 86400
            except Exception:
                pass
        if stale and scheduler:
            import asyncio
            asyncio.ensure_future(scheduler.trigger("digest"))
    except Exception:
        pass

    # Agent identity — age + XP
    agent_identity = {"born_at": "", "total_xp": 0, "age_days": 0}
    try:
        from ...agent.identity import get_identity
        agent_identity = await get_identity()
    except Exception:
        pass

    return request.app.state.templates.TemplateResponse(request, "overview.html", {
        "stats": stats,
        "model_status": model_status,
        "skills_count": skills_count,
        "skills_enabled": skills_enabled,
        "scheduler_jobs": scheduler_jobs,
        "snapshots_count": len(snapshots),
        "timezone": tz,
        "now": now_str,
        "perf_summary": perf_summary,
        "econ_today": econ_today,
        "heatmap": heatmap,
        "streak": streak,
        "digest": digest,
        "agent_identity": agent_identity,
    })
