"""Health & Introspection dashboard route."""

import json
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()
logger = structlog.get_logger()


@router.get("/", response_class=HTMLResponse)
async def health_page(request: Request):
    hm = request.app.state.health_monitor
    introspector = request.app.state.introspector

    latest = None
    history = []
    perf = None
    caps = None
    score = None

    if hm:
        latest = await hm.get_latest()
        history = await hm.get_history(count=12)

    if introspector:
        perf = await introspector.get_performance(hours=24)
        caps = introspector.get_capabilities()
        score = await introspector.compute_health_score()

    # Build sparkline data arrays from history (oldest first for left-to-right rendering)
    hist_rev = list(reversed(history))
    disk_spark = [h.get("system", {}).get("disk", {}).get("percent", 0) for h in hist_rev]
    ram_spark  = [h.get("system", {}).get("ram", {}).get("percent", 0) for h in hist_rev]
    cpu_spark  = [h.get("system", {}).get("cpu", {}).get("percent", 0) for h in hist_rev]

    disk_spark_json = json.dumps(disk_spark)
    ram_spark_json  = json.dumps(ram_spark)
    cpu_spark_json  = json.dumps(cpu_spark)

    # ── Safety & Execution Control data ──────────────────────────────────────
    safety_data = {
        "gate_decision": None,
        "gate_reason": None,
        "gate_ts": None,
        "crit3_count": 0,
        "crit3_limit": 3,
        "saccadic_last": None,
        "saccadic_hash": None,
    }
    try:
        from ...db.models import AuditLog
        from ...db.session import async_session
        from sqlalchemy import select, desc as _desc
        async with async_session() as session:
            row = await session.execute(
                select(AuditLog.error, AuditLog.output_summary, AuditLog.timestamp)
                .where(AuditLog.action == "skill.self_improve")
                .order_by(_desc(AuditLog.timestamp))
                .limit(1)
            )
            entry = row.first()
            if entry:
                err, out, ts = entry
                if err:
                    safety_data["gate_decision"] = "BLOCK"
                    safety_data["gate_reason"] = str(err)[:80]
                elif out and "warn" in (out or "").lower():
                    safety_data["gate_decision"] = "WARN"
                    safety_data["gate_reason"] = (out or "")[:80]
                else:
                    safety_data["gate_decision"] = "ALLOW"
                    safety_data["gate_reason"] = (out or "")[:80]
                safety_data["gate_ts"] = ts.strftime("%Y-%m-%d %H:%M") if ts else None
    except Exception:
        logger.exception("health_page.safety_audit_error")

    behavioral_pending = 0
    try:
        import redis.asyncio as aioredis
        _r = aioredis.from_url(request.app.state.redis_url, decode_responses=True, socket_connect_timeout=1)
        try:
            today = datetime.now(timezone.utc).strftime("%Y%m%d")
            count_raw = await _r.get(f"self_improve:daily_cap:{today}")
            safety_data["crit3_count"] = int(count_raw or 0)
            events = await _r.xrevrange("events:saccadic", count=1)
            if events:
                _, fields = events[0]
                raw_ts = fields.get("timestamp", "")
                safety_data["saccadic_last"] = raw_ts[:16].replace("T", " ") if raw_ts else None
                safety_data["saccadic_hash"] = fields.get("curr_hash", "")[:8] or None
            behavioral_pending = await _r.llen("behavioral:pending")
        finally:
            await _r.aclose()
    except Exception:
        logger.exception("health_page.safety_redis_error")

    # ── Integrity report (moved from Cognitive) ───────────────────────────────
    integrity_report = {}
    try:
        import redis.asyncio as aioredis
        _ri = aioredis.from_url(request.app.state.redis_url, decode_responses=True, socket_connect_timeout=1)
        try:
            raw_ir = await _ri.get("agent:integrity_report")
            if raw_ir:
                integrity_report = json.loads(raw_ir)
        finally:
            await _ri.aclose()
    except Exception:
        logger.exception("health_page.integrity_report_error")

    return request.app.state.templates.TemplateResponse(request, "health.html", {
        "latest": latest,
        "history": history,
        "perf": perf,
        "caps": caps,
        "score": score,
        "disk_spark": disk_spark_json,
        "ram_spark": ram_spark_json,
        "cpu_spark": cpu_spark_json,
        "safety_data": safety_data,
        "integrity_report": integrity_report,
        "behavioral_pending": behavioral_pending,
    })


@router.get("/api/latest")
async def health_api_latest(request: Request):
    """Live health snapshot for auto-refresh — returns latest + score + perf summary."""
    hm = request.app.state.health_monitor
    introspector = request.app.state.introspector

    latest = None
    history = []
    score = None
    perf = None

    try:
        if hm:
            latest = await hm.get_latest()
            history = await hm.get_history(count=12)
        if introspector:
            score = await introspector.compute_health_score()
            perf_full = await introspector.get_performance(hours=24)
            perf = {
                "total_events": perf_full["total_events"],
                "avg_latency_ms": perf_full["avg_latency_ms"],
                "errors": perf_full["errors"],
                "error_rate": perf_full["error_rate"],
                "hourly": perf_full["hourly"],
            }
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)[:120]}, status_code=500)

    return JSONResponse({
        "ok": True,
        "latest": latest,
        "history": history,
        "score": score,
        "perf": perf,
    })
