"""Opportunities API — Phase C proactive opportunity feed.

Exposes detected opportunities (stored by OpportunityEngine) for dashboard
consumption.  No UI page — pure JSON API reused by the Goals/Insights sections.

Routes:
    GET  /opportunities/api/list          — all opportunities, newest first
    GET  /opportunities/api/stats         — counts by status and source
    POST /opportunities/api/{id}/status   — update status (seen/accepted/rejected)
"""
from __future__ import annotations

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()
logger = structlog.get_logger()


@router.get("/api/list")
async def list_opportunities(request: Request, status: str = "", limit: int = 50):
    """Return stored opportunities, optionally filtered by status."""
    try:
        from ...db.models import Opportunity
        from ...db.session import async_session
        from sqlalchemy import select, desc

        async with async_session() as session:
            stmt = select(Opportunity).order_by(desc(Opportunity.created_at)).limit(min(limit, 100))
            if status:
                stmt = select(Opportunity).where(Opportunity.status == status).order_by(
                    desc(Opportunity.created_at)
                ).limit(min(limit, 100))
            rows = (await session.execute(stmt)).scalars().all()

        items = [
            {
                "id":               o.id,
                "type":             o.opp_type,
                "description":      o.description,
                "confidence":       round(float(o.confidence or 0), 2),
                "source":           o.source,
                "action_policy":    o.action_policy,
                "status":           o.status,
                "related_entities": o.related_entities or [],
                "created_at":       o.created_at.isoformat() if o.created_at else "",
                "suggested_at":     o.suggested_at.isoformat() if o.suggested_at else None,
            }
            for o in rows
        ]
        return JSONResponse({"ok": True, "opportunities": items, "count": len(items)})

    except Exception as exc:
        logger.warning("opportunities.list_failed", error=str(exc)[:120])
        return JSONResponse({"ok": False, "error": str(exc)[:120]}, status_code=500)


@router.get("/api/stats")
async def opportunity_stats(request: Request):
    """Summary counts by status and source."""
    try:
        from ...db.models import Opportunity
        from ...db.session import async_session
        from sqlalchemy import select, func

        async with async_session() as session:
            rows = (await session.execute(select(Opportunity))).scalars().all()

        by_status: dict[str, int] = {}
        by_source: dict[str, int] = {}
        by_type: dict[str, int] = {}
        for o in rows:
            by_status[o.status] = by_status.get(o.status, 0) + 1
            by_source[o.source] = by_source.get(o.source, 0) + 1
            by_type[o.opp_type] = by_type.get(o.opp_type, 0) + 1

        return JSONResponse({
            "ok": True,
            "total": len(rows),
            "by_status": by_status,
            "by_source": by_source,
            "by_type": by_type,
        })

    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)[:120]}, status_code=500)


@router.post("/api/{opportunity_id}/status")
async def update_opportunity_status(request: Request, opportunity_id: str):
    """Update opportunity status (seen / accepted / rejected)."""
    _VALID_STATUSES = {"seen", "accepted", "rejected"}
    try:
        body = await request.json()
        new_status = str(body.get("status", "")).strip()
        if new_status not in _VALID_STATUSES:
            return JSONResponse(
                {"ok": False, "error": f"Invalid status. Allowed: {sorted(_VALID_STATUSES)}"},
                status_code=400,
            )

        from ...db.models import Opportunity
        from ...db.session import async_session

        async with async_session() as session:
            rec = await session.get(Opportunity, opportunity_id)
            if not rec:
                return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
            rec.status = new_status
            await session.commit()

        logger.info("opportunity.status_updated", id=opportunity_id, status=new_status)
        return JSONResponse({"ok": True, "id": opportunity_id, "status": new_status})

    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)[:120]}, status_code=500)
