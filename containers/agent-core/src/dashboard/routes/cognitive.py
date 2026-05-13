"""Cognitive State Dashboard — Self-Model, Epistemic state, CPI.

Tabs:
  - Self-Model  : strengths, failures, preferences, skill rates, improvement queue
  - Epistemic   : calibrated domain confidence bars
  - CPI         : Cognitive Pressure Index live gauge

Moved to dedicated pages:
  - Behavioral Rules  → /behavioral-rules
  - Self-Improve      → /self-improve
  - Knowledge Graph   → /knowledge-graph
  - Integrity Report  → /health (Integrity tab)
"""
from __future__ import annotations

import json

import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import func, select, desc

from ...db.models import BehavioralRule, LearningExample
from ...db.session import async_session

router = APIRouter()
logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
async def cognitive_page(request: Request):
    redis_url = request.app.state.redis_url

    # --- Epistemic state ---
    epistemic = {}
    try:
        from ...agent.epistemic import load as ep_load
        epistemic = await ep_load(redis_url)
    except Exception:
        logger.exception("cognitive_page.epistemic_load_error")

    # --- Self-model ---
    self_model = {}
    try:
        from ...agent.self_model import load as sm_load
        self_model = await sm_load(redis_url)
    except Exception:
        logger.exception("cognitive_page.self_model_load_error")

    # --- Cognitive Pressure Index ---
    cpi_report = {}
    try:
        from ...agent.cpi import load as cpi_load
        cpi_report = await cpi_load(redis_url)
    except Exception:
        logger.exception("cognitive_page.cpi_load_error")

    # --- Compute skill success rates for display ---
    skill_rates = []
    raw_rates = self_model.get("skill_success_rates", {})
    for skill, counts in raw_rates.items():
        total = counts.get("success", 0) + counts.get("failure", 0)
        if total == 0:
            continue
        pct = round(counts["success"] / total * 100)
        skill_rates.append({
            "skill": skill,
            "success": counts["success"],
            "failure": counts["failure"],
            "total": total,
            "pct": pct,
        })
    skill_rates.sort(key=lambda x: x["total"], reverse=True)

    # --- Self-improve proposals ---
    proposals = []
    proposal_count = 0
    try:
        r = aioredis.from_url(redis_url, decode_responses=True)
        try:
            raw_proposals = await r.hgetall("self_improve:proposals")
        finally:
            await r.aclose()
        for pid, pjson in raw_proposals.items():
            try:
                p = json.loads(pjson)
                p.setdefault("id", pid)
                diff = p.get("diff", "")
                p["diff_preview"] = diff[:300] if diff else ""
                proposals.append(p)
            except Exception:
                logger.exception("cognitive_page.proposal_parse_error", pid=pid)
        proposals.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        proposal_count = len(proposals)
    except Exception:
        logger.exception("cognitive_page.proposals_load_error")

    # --- Behavioral rules ---
    behavioral_rules = []
    behavioral_rule_count = 0
    try:
        async with async_session() as session:
            r = await session.execute(
                select(BehavioralRule).order_by(desc(BehavioralRule.created_at)).limit(50)
            )
            behavioral_rules = list(r.scalars().all())
            behavioral_rule_count = len(behavioral_rules)
    except Exception:
        logger.exception("cognitive_page.behavioral_rules_load_error")

    # --- Learning examples ---
    learning_examples = []
    learning_example_count = 0
    try:
        async with async_session() as session:
            r = await session.execute(
                select(LearningExample).order_by(desc(LearningExample.use_count)).limit(50)
            )
            learning_examples = list(r.scalars().all())
            cnt = await session.execute(select(func.count(LearningExample.id)))
            learning_example_count = cnt.scalar() or 0
    except Exception:
        logger.exception("cognitive_page.learning_examples_load_error")

    # --- Integrity report ---
    integrity_report = {}
    try:
        r = aioredis.from_url(redis_url, decode_responses=True)
        try:
            raw = await r.get("agent:integrity_report")
        finally:
            await r.aclose()
        if raw:
            integrity_report = json.loads(raw)
    except Exception:
        logger.exception("cognitive_page.integrity_report_load_error")

    return request.app.state.templates.TemplateResponse(request, "cognitive.html", {
        "epistemic":              epistemic,
        "self_model":             self_model,
        "skill_rates":            skill_rates,
        "cpi_report":             cpi_report,
        "proposals":              proposals,
        "proposal_count":         proposal_count,
        "behavioral_rules":       behavioral_rules,
        "behavioral_rule_count":  behavioral_rule_count,
        "learning_examples":      learning_examples,
        "learning_example_count": learning_example_count,
        "integrity_report":       integrity_report,
    })


@router.post("/api/epistemic/reset")
async def reset_epistemic(request: Request):
    """Reset epistemic state to defaults."""
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(request.app.state.redis_url, decode_responses=True)
        from ...agent.epistemic import EPISTEMIC_KEY
        await r.delete(EPISTEMIC_KEY)
        await r.aclose()
        logger.info("cognitive.epistemic_reset")
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e).splitlines()[0][:120]}, status_code=500)


@router.get("/api/cpi")
async def cpi_snapshot(request: Request):
    """Live CPI snapshot for auto-refresh."""
    try:
        from ...agent.cpi import load as cpi_load
        cpi = await cpi_load(request.app.state.redis_url)
        return JSONResponse({"ok": True, "cpi": cpi})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e).splitlines()[0][:120]}, status_code=500)


@router.get("/api/impact")
async def impact_trail(request: Request, count: int = 50):
    """Return the cognitive impact log — which systems influenced recent decisions."""
    try:
        from ...agent.impact_tracker import get_recent_impacts, get_impact_stats
        redis_url = request.app.state.redis_url
        impacts = await get_recent_impacts(redis_url, count=min(count, 200))
        stats = await get_impact_stats(redis_url)
        return JSONResponse({"ok": True, "impacts": impacts, "stats": stats})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e).splitlines()[0][:120]}, status_code=500)


@router.get("/api/disk")
async def disk_usage(request: Request):
    """Return browser session and screenshot disk usage for operator visibility."""
    import os
    from pathlib import Path

    def _size_gb(path: Path) -> float:
        try:
            total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            return round(total / (1024 ** 3), 3)
        except Exception:
            return 0.0

    def _count(path: Path) -> int:
        try:
            return sum(1 for _ in path.iterdir())
        except Exception:
            return 0

    browser_dir = Path("/data/browser_sessions")
    screenshots_dir = Path("/data/screenshots")

    # Overall disk
    disk_total_gb = disk_used_gb = 0.0
    try:
        stat = os.statvfs("/data")
        disk_total_gb = round(stat.f_frsize * stat.f_blocks / (1024**3), 1)
        disk_used_gb = round((stat.f_frsize * (stat.f_blocks - stat.f_bfree)) / (1024**3), 1)
    except Exception:
        pass

    return JSONResponse({
        "ok": True,
        "browser_sessions": {
            "path": str(browser_dir),
            "size_gb": _size_gb(browser_dir),
            "session_count": _count(browser_dir),
            "cap_gb": 20.0,
        },
        "screenshots": {
            "path": str(screenshots_dir),
            "size_gb": _size_gb(screenshots_dir),
            "file_count": _count(screenshots_dir),
            "cap_gb": 2.0,
        },
        "disk": {
            "total_gb": disk_total_gb,
            "used_gb": disk_used_gb,
            "used_pct": round(disk_used_gb / disk_total_gb * 100, 1) if disk_total_gb else 0.0,
        },
    })


