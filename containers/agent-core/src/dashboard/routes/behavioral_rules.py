"""Behavioral Rules — governance view of learned correction rules."""
from __future__ import annotations

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import func, select, desc

router = APIRouter()
logger = structlog.get_logger()


@router.get("/", response_class=HTMLResponse)
async def behavioral_rules_page(request: Request):
    rules = []
    learning_examples = []
    rule_count = 0
    active_count = 0
    type_counts: dict = {}

    try:
        from ...db.models import BehavioralRule
        from ...db.session import async_session
        async with async_session() as session:
            # Real totals from DB (not from the limited slice)
            total_res = await session.execute(select(func.count(BehavioralRule.id)))
            rule_count = total_res.scalar() or 0
            active_res = await session.execute(
                select(func.count(BehavioralRule.id)).where(BehavioralRule.active == True)  # noqa: E712
            )
            active_count = active_res.scalar() or 0
            type_res = await session.execute(
                select(BehavioralRule.rule_type, func.count(BehavioralRule.id))
                .group_by(BehavioralRule.rule_type)
            )
            for rule_type, cnt in type_res.all():
                type_counts[rule_type] = cnt

            result = await session.execute(
                select(BehavioralRule).order_by(desc(BehavioralRule.created_at)).limit(100)
            )
            for br in result.scalars().all():
                rules.append({
                    "id": br.id,
                    "rule_type": br.rule_type,
                    "description": br.description,
                    "confidence": round(float(br.confidence or 0), 2),
                    "active": br.active,
                    "times_applied": br.times_applied or 0,
                    "skill_poison": br.skill_poison,
                    "created_at": br.created_at.strftime("%Y-%m-%d %H:%M") if br.created_at else "",
                })
    except Exception:
        logger.exception("behavioral_rules_page.rules_load_error")

    try:
        from ...db.models import LearningExample
        from ...db.session import async_session
        async with async_session() as session:
            result = await session.execute(
                select(LearningExample).order_by(desc(LearningExample.use_count)).limit(30)
            )
            for ex in result.scalars().all():
                learning_examples.append({
                    "id": ex.id,
                    "user_input": (ex.user_input or "")[:180],
                    "skill_calls": (ex.skill_calls or "")[:120],
                    "outcome": ex.outcome or "positive",
                    "use_count": ex.use_count or 0,
                })
    except Exception:
        logger.exception("behavioral_rules_page.examples_load_error")

    return request.app.state.templates.TemplateResponse(request, "behavioral_rules.html", {
        "rules": rules,
        "learning_examples": learning_examples,
        "rule_count": rule_count,
        "active_count": active_count,
        "type_counts": type_counts,
    })


@router.post("/api/{rule_id}/toggle")
async def toggle_rule(request: Request, rule_id: int):
    try:
        from ...db.models import BehavioralRule
        from ...db.session import async_session
        async with async_session() as session:
            rule = await session.get(BehavioralRule, rule_id)
            if not rule:
                return JSONResponse({"ok": False, "error": "Rule not found"}, status_code=404)
            rule.active = not rule.active
            await session.commit()
            logger.info("behavioral_rules.toggled", rule_id=rule_id, active=rule.active)
            return JSONResponse({"ok": True, "active": rule.active})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e).splitlines()[0][:120]}, status_code=500)


@router.delete("/api/{rule_id}")
async def delete_rule(request: Request, rule_id: int):
    try:
        from ...db.models import BehavioralRule
        from ...db.session import async_session
        async with async_session() as session:
            rule = await session.get(BehavioralRule, rule_id)
            if not rule:
                return JSONResponse({"ok": False, "error": "Rule not found"}, status_code=404)
            await session.delete(rule)
            await session.commit()
            logger.info("behavioral_rules.deleted", rule_id=rule_id)
            return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e).splitlines()[0][:120]}, status_code=500)
