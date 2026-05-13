"""Dashboard route — Skill Evolution log (System 5)."""
from __future__ import annotations

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select, func

router = APIRouter()
logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# HTML page — SSR stats for instant first paint
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
async def skill_evolution_page(request: Request):
    from ...db.session import async_session
    from ...db.models import SkillPattern
    from ...config import settings

    stats = {
        "total": 0,
        "synthesized": 0,
        "pending": 0,
        "threshold": getattr(settings, "skill_pattern_threshold", 5),
        "enabled": getattr(settings, "skill_evolution_enabled", False),
        "patterns": [],
    }
    try:
        async with async_session() as session:
            stats["total"] = (
                await session.execute(select(func.count()).select_from(SkillPattern))
            ).scalar() or 0

            stats["synthesized"] = (
                await session.execute(
                    select(func.count()).select_from(SkillPattern)
                    .where(SkillPattern.synthesized == True)  # noqa: E712
                )
            ).scalar() or 0

            stats["pending"] = stats["total"] - stats["synthesized"]

            rows = (await session.execute(
                select(SkillPattern).order_by(SkillPattern.occurrence_count.desc()).limit(50)
            )).scalars().all()

            stats["patterns"] = [
                {
                    "id": r.id,
                    "pattern_key": r.pattern_key,
                    "skill_sequence": r.skill_sequence or [],
                    "occurrence_count": r.occurrence_count,
                    "synthesized": r.synthesized,
                    "composite_skill_name": r.composite_skill_name or "",
                    "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else "",
                    "created_at": r.created_at.isoformat() if r.created_at else "",
                }
                for r in rows
            ]
    except Exception:
        logger.exception("skill_evolution_page.load_error")

    return request.app.state.templates.TemplateResponse(
        request, "skill_evolution.html", {"stats": stats}
    )


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

@router.get("/api/stats")
async def get_stats(request: Request):
    """Return summary stats — used by auto-refresh."""
    from ...db.session import async_session
    from ...db.models import SkillPattern
    from ...config import settings
    try:
        async with async_session() as session:
            total = (
                await session.execute(select(func.count()).select_from(SkillPattern))
            ).scalar() or 0
            synthesized = (
                await session.execute(
                    select(func.count()).select_from(SkillPattern)
                    .where(SkillPattern.synthesized == True)  # noqa: E712
                )
            ).scalar() or 0
        return JSONResponse({
            "ok": True,
            "total": total,
            "synthesized": synthesized,
            "pending": total - synthesized,
            "threshold": getattr(settings, "skill_pattern_threshold", 5),
            "enabled": getattr(settings, "skill_evolution_enabled", False),
        })
    except Exception as exc:
        logger.exception("skill_evolution.stats_error")
        return JSONResponse({"ok": False, "error": str(exc)[:120]})


@router.get("/api/patterns")
async def get_patterns(request: Request):
    """Return detected skill patterns."""
    from ...db.session import async_session
    from ...db.models import SkillPattern
    try:
        async with async_session() as session:
            rows = (await session.execute(
                select(SkillPattern).order_by(SkillPattern.occurrence_count.desc()).limit(50)
            )).scalars().all()

        return JSONResponse({
            "ok": True,
            "patterns": [
                {
                    "id": r.id,
                    "pattern_key": r.pattern_key,
                    "skill_sequence": r.skill_sequence or [],
                    "occurrence_count": r.occurrence_count,
                    "synthesized": r.synthesized,
                    "composite_skill_name": r.composite_skill_name or "",
                    "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else "",
                    "created_at": r.created_at.isoformat() if r.created_at else "",
                }
                for r in rows
            ],
        })
    except Exception as exc:
        logger.exception("skill_evolution.patterns_error")
        return JSONResponse({"ok": False, "patterns": [], "error": str(exc)[:120]})


@router.post("/api/run")
async def run_evolution_cycle(request: Request):
    """Manually trigger a skill evolution cycle."""
    from ...config import settings
    if not getattr(settings, "skill_evolution_enabled", False):
        return JSONResponse({"ok": False, "error": "SKILL_EVOLUTION_ENABLED=false — enable it in settings to use this feature"})

    try:
        model_manager = request.app.state.model_manager
        from ...db.session import async_session
        from ...skills.skill_evolution import SkillEvolutionEngine

        engine = SkillEvolutionEngine(
            model_manager=model_manager,
            min_pattern_count=getattr(settings, "skill_pattern_threshold", 5),
        )
        async with async_session() as session:
            result = await engine.run(session)
        return JSONResponse({"ok": True, **result})
    except Exception as exc:
        logger.exception("skill_evolution.run_error")
        return JSONResponse({"ok": False, "error": str(exc)[:120]})
