"""Agent State Badge — lightweight htmx fragment endpoint.

Returns a small HTML fragment showing current agent state.
Polled every 10s via htmx hx-trigger="load, every 10s".

State priority:
  1. DREAM    — dream cycle active (agent:dream_state)
  2. LIGHT    — CPI/CPU/latency thresholds exceeded (agent:cpi)
  3. EXECUTING — active goal exists in Redis goals HASH
  4. SKILL    — skill execution in audit_log within last 30s
  5. IDLE     — default fallback

Fail-open: any exception → IDLE (never blocks page render).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import desc, select

from ...db.models import AuditLog
from ...db.session import async_session

logger = structlog.get_logger()
router = APIRouter()

# ── State constants ───────────────────────────────────��────────────────────────
_STATES = ("IDLE", "EXECUTING", "SKILL", "LIGHT", "DREAM")

# CPI thresholds that trigger LIGHT mode
_CPU_THRESHOLD     = 80.0
_LATENCY_THRESHOLD = 500.0
_CPI_THRESHOLD     = 80.0

# How recent a skill call must be to count as SKILL state
_SKILL_WINDOW_S = 30


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Core state resolver ────────────────────────────────────────────────────────

async def get_agent_state(redis_url: str) -> dict:
    """Compute current agent state. Always returns a valid dict. Never raises."""
    state     = "IDLE"
    reason    = "No active operations"
    light_mode = False

    # ── 1. Redis signals (dream + CPI) ────────────────────��───────────────────
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(redis_url, decode_responses=True, socket_connect_timeout=1)
        try:
            # 1a. Dream cycle?
            dream_raw = await r.get("agent:dream_state")
            if dream_raw:
                try:
                    dream_data = json.loads(dream_raw)
                    if dream_data.get("active"):
                        return {
                            "state":      "DREAM",
                            "reason":     "Dream consolidation cycle running",
                            "light_mode": False,
                            "timestamp":  _now_iso(),
                        }
                except Exception:
                    pass

            # 1b. CPI / light mode?
            cpi_raw = await r.get("agent:cpi")
            if cpi_raw:
                try:
                    cpi = json.loads(cpi_raw)
                    cpu     = float(cpi.get("cpu_percent", 0) or 0)
                    latency = float(cpi.get("avg_latency_ms", 0) or 0)
                    cpi_val = float(cpi.get("cpi", 0) or 0)
                    if cpu > _CPU_THRESHOLD or latency > _LATENCY_THRESHOLD or cpi_val > _CPI_THRESHOLD:
                        light_mode = True
                except Exception:
                    pass

            # 1c. Active goals in Redis HASH?
            try:
                from ...goal_orchestrator.store import list_active_goals
                active_goals = await list_active_goals(r)
                if active_goals:
                    g = active_goals[0]
                    current_task = (
                        g.task_graph.get_next_task() if g.task_graph else None
                    )
                    skill = current_task.skill_name if current_task else ""
                    title = g.title or (g.objective[:40] if g.objective else "goal")
                    if skill:
                        state  = "SKILL"
                        reason = f"Skill: {skill}"
                    else:
                        state  = "EXECUTING"
                        reason = f"Goal: {title}"
                    if light_mode:
                        state  = "LIGHT"
                        reason += " (light mode)"
                    return {
                        "state":      state,
                        "reason":     reason,
                        "light_mode": light_mode,
                        "timestamp":  _now_iso(),
                    }
            except Exception:
                pass

        finally:
            await r.aclose()
    except Exception:
        pass

    # ── 2. Recent skill execution in audit_log (< 30s) ────────────────────────
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=_SKILL_WINDOW_S)
        async with async_session() as session:
            row = await session.execute(
                select(AuditLog.action, AuditLog.event_type)
                .where(AuditLog.timestamp >= cutoff)
                .where(AuditLog.action.isnot(None))
                .order_by(desc(AuditLog.timestamp))
                .limit(1)
            )
            entry = row.first()
            if entry:
                action, etype = entry
                etype_lower = (etype or "").lower()
                if "skill" in etype_lower:
                    state  = "SKILL"
                    reason = f"Skill: {action or 'executing'}"
                elif "telegram" in etype_lower or "message" in etype_lower:
                    state  = "EXECUTING"
                    reason = "Processing message"
    except Exception:
        pass

    # ── 3. Apply light mode if still IDLE ─────────────────────────────────────
    if light_mode and state == "IDLE":
        state  = "LIGHT"
        reason = "High system load — light execution mode"

    return {
        "state":      state,
        "reason":     reason,
        "light_mode": light_mode,
        "timestamp":  _now_iso(),
    }


# ── Last safety gate decision ──────────────────────────────────────────────────

async def _get_last_safety() -> str:
    """Return last self_improve safety outcome from audit_log. Fail-open → unknown."""
    try:
        async with async_session() as session:
            row = await session.execute(
                select(AuditLog.error, AuditLog.output_summary)
                .where(AuditLog.action == "self_improve")
                .order_by(desc(AuditLog.timestamp))
                .limit(1)
            )
            entry = row.first()
            if entry:
                err, out = entry
                if err:
                    return "BLOCK"
                if out and "gate" in (out or "").lower():
                    return "WARN"
                return "ALLOW"
    except Exception:
        pass
    return "unknown"


# ── HTML fragment template ─────────────────────────────────────────────────────

_BADGE_HTML = """\
<div id="agent-state-badge"
     hx-get="/state/badge"
     hx-trigger="every 10s"
     hx-swap="outerHTML"
     class="px-3 py-2 border-t border-base-300/30">
  <div class="flex items-center gap-1.5">
    <span class="wasp-state-dot wasp-state-dot-{css_state}"></span>
    <span class="wasp-state-label wasp-state-{css_state}">{state}</span>
  </div>
  <div class="wasp-state-reason" title="{reason}">{reason_short}</div>
  <div class="wasp-state-meta">Safety: {safety} &middot; {ts}</div>
</div>"""


@router.get("/badge", response_class=HTMLResponse)
async def state_badge(request: Request):
    redis_url = request.app.state.redis_url
    state_data = await get_agent_state(redis_url)
    safety     = await _get_last_safety()

    state  = state_data["state"]
    reason = state_data["reason"]
    ts     = state_data["timestamp"][11:16]   # HH:MM UTC

    css_state    = state.lower()
    reason_short = (reason[:26] + "…") if len(reason) > 26 else reason

    html = _BADGE_HTML.format(
        css_state    = css_state,
        state        = state,
        reason       = reason,
        reason_short = reason_short,
        safety       = safety,
        ts           = ts,
    )

    logger.info("ui.agent_state_polled", state=state, light_mode=state_data["light_mode"])
    return HTMLResponse(html)


@router.get("/json", response_class=JSONResponse)
async def state_json(request: Request):
    """Lightweight JSON state endpoint — polled by overview canvas every 10s."""
    redis_url  = request.app.state.redis_url
    state_data = await get_agent_state(redis_url)
    cpi_val    = 0.0
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(redis_url, decode_responses=True, socket_connect_timeout=1)
        raw = await r.get("agent:cpi")
        await r.aclose()
        if raw:
            import json as _json
            cpi_val = float(_json.loads(raw).get("cpi", 0) or 0)
    except Exception:
        pass
    return JSONResponse({
        "state": state_data["state"],
        "cpi":   round(cpi_val, 1),
        "reason": state_data["reason"],
    })
