"""Dashboard route — World Model viewer (System 4)."""
from __future__ import annotations

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select, func

router = APIRouter()
logger = structlog.get_logger()

_CRYPTO_SYMBOLS = {
    "btc", "eth", "sol", "ada", "bnb", "xrp", "doge", "avax", "dot", "matic",
    "link", "ltc", "xlm", "atom", "algo", "near", "ftm", "sand", "mana", "uni",
}


def _infer_entity_type(entity: str, stored_type: str) -> str:
    if stored_type and stored_type != "generic":
        return stored_type
    el = entity.lower()
    if el in _CRYPTO_SYMBOLS:
        return "crypto"
    if "price" in el or "usd" in el:
        return "crypto"
    if el in {"user_state", "user", "agent", "system"}:
        return "system"
    if el in {"cpu", "ram", "disk", "memory", "latency"}:
        return "metric"
    return "generic"


def _fmt_entity(r, latest_obs: str = "", sparkline: list | None = None) -> dict:
    entity_type = _infer_entity_type(r.entity, r.entity_type)
    return {
        "entity": r.entity,
        "entity_type": entity_type,
        "current_value": r.current_value or "",
        "previous_value": r.previous_value or "",
        "change_pct": round(r.change_pct, 2) if r.change_pct is not None else None,
        "trend": r.trend or "unknown",
        "last_updated": r.last_updated.isoformat() if r.last_updated else "",
        "observation_count": (r.state_metadata or {}).get("observation_count", 0),
        "latest_obs": latest_obs,
        "sparkline": sparkline or [],
    }


async def _load_timeline_extras(session, entity_names: list[str]) -> tuple[dict, dict]:
    """Batch-load latest_obs and sparkline data for a list of entities."""
    from ...db.models import WorldTimeline
    from datetime import datetime, timezone, timedelta

    if not entity_names:
        return {}, {}

    # Latest observation timestamp per entity (freshness indicator)
    latest_q = await session.execute(
        select(WorldTimeline.entity, func.max(WorldTimeline.observed_at).label("latest"))
        .where(WorldTimeline.entity.in_(entity_names))
        .group_by(WorldTimeline.entity)
    )
    latest_obs = {r.entity: r.latest.isoformat() for r in latest_q.all()}

    # Sparkline values: last 14 days, max 60 per entity, ordered by time
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    spark_q = await session.execute(
        select(WorldTimeline.entity, WorldTimeline.value, WorldTimeline.observed_at)
        .where(
            WorldTimeline.entity.in_(entity_names),
            WorldTimeline.observed_at >= cutoff,
        )
        .order_by(WorldTimeline.entity, WorldTimeline.observed_at.asc())
    )
    sparklines: dict[str, list[str]] = {}
    for row in spark_q.all():
        sparklines.setdefault(row.entity, []).append(row.value)
    # Downsample to max 60 points per entity
    for ent in sparklines:
        pts = sparklines[ent]
        if len(pts) > 60:
            step = len(pts) / 60
            sparklines[ent] = [pts[int(i * step)] for i in range(60)]

    return latest_obs, sparklines


# ---------------------------------------------------------------------------
# HTML page
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
async def world_model_page(request: Request):
    from ...db.session import async_session
    from ...db.models import EntityState, StatePrediction
    from datetime import datetime, timezone

    entity_count = 0
    prediction_count = 0
    last_sync = ""
    entities = []

    try:
        now = datetime.now(timezone.utc)
        async with async_session() as session:
            entity_count = (await session.execute(
                select(func.count()).select_from(EntityState)
            )).scalar() or 0

            prediction_count = (await session.execute(
                select(func.count()).select_from(StatePrediction).where(
                    (StatePrediction.expires_at == None)  # noqa: E711
                    | (StatePrediction.expires_at > now)
                )
            )).scalar() or 0

            last_row = (await session.execute(
                select(EntityState.last_updated)
                .order_by(EntityState.last_updated.desc()).limit(1)
            )).scalar()
            if last_row:
                last_sync = last_row.isoformat()

            rows = (await session.execute(
                select(EntityState).order_by(EntityState.last_updated.desc()).limit(50)
            )).scalars().all()

            entity_names = [r.entity for r in rows]
            latest_obs, sparklines = await _load_timeline_extras(session, entity_names)
            entities = [
                _fmt_entity(r, latest_obs.get(r.entity, ""), sparklines.get(r.entity))
                for r in rows
            ]
    except Exception:
        logger.exception("world_model_page.load_error")

    return request.app.state.templates.TemplateResponse(
        request, "world_model.html", {
            "entity_count": entity_count,
            "prediction_count": prediction_count,
            "last_sync": last_sync,
            "entities": entities,
        }
    )


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

@router.get("/api/stats")
async def get_stats(request: Request):
    from ...db.session import async_session
    from ...db.models import EntityState, StatePrediction
    from datetime import datetime, timezone
    try:
        now = datetime.now(timezone.utc)
        async with async_session() as session:
            ec = (await session.execute(
                select(func.count()).select_from(EntityState)
            )).scalar() or 0
            pc = (await session.execute(
                select(func.count()).select_from(StatePrediction).where(
                    (StatePrediction.expires_at == None)  # noqa: E711
                    | (StatePrediction.expires_at > now)
                )
            )).scalar() or 0
            last_row = (await session.execute(
                select(EntityState.last_updated)
                .order_by(EntityState.last_updated.desc()).limit(1)
            )).scalar()
        return JSONResponse({
            "ok": True,
            "entity_count": ec,
            "prediction_count": pc,
            "last_sync": last_row.isoformat() if last_row else "",
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)[:120]})


@router.get("/api/entities")
async def get_entity_states(request: Request):
    """Return entity states with sparkline data and freshness."""
    try:
        from ...db.session import async_session
        from ...db.models import EntityState

        async with async_session() as session:
            rows = (await session.execute(
                select(EntityState).order_by(EntityState.last_updated.desc()).limit(50)
            )).scalars().all()

            entity_names = [r.entity for r in rows]
            latest_obs, sparklines = await _load_timeline_extras(session, entity_names)

        return JSONResponse({
            "ok": True,
            "entities": [
                _fmt_entity(r, latest_obs.get(r.entity, ""), sparklines.get(r.entity))
                for r in rows
            ],
        })
    except Exception as exc:
        logger.exception("world_model.entities_error")
        return JSONResponse({"ok": False, "entities": [], "error": str(exc)[:120]})


@router.get("/api/entities/{entity}/history")
async def get_entity_history(request: Request, entity: str):
    """Full observation history for a single entity (last 14 days)."""
    try:
        from ...db.session import async_session
        from ...db.models import WorldTimeline
        from datetime import datetime, timezone, timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(days=14)
        async with async_session() as session:
            rows = (await session.execute(
                select(WorldTimeline)
                .where(WorldTimeline.entity == entity, WorldTimeline.observed_at >= cutoff)
                .order_by(WorldTimeline.observed_at.asc())
                .limit(200)
            )).scalars().all()

        return JSONResponse({
            "ok": True,
            "entity": entity,
            "history": [
                {
                    "ts": r.observed_at.isoformat(),
                    "value": r.value,
                    "type": r.observation_type,
                }
                for r in rows
            ],
        })
    except Exception as exc:
        logger.exception("world_model.history_error")
        return JSONResponse({"ok": False, "history": [], "error": str(exc)[:120]})


@router.post("/api/entities/{entity}/forecast")
async def generate_entity_forecast(request: Request, entity: str):
    """Generate an LLM forecast for an entity on demand."""
    model_manager = getattr(request.app.state, "model_manager", None)
    if not model_manager:
        return JSONResponse({"ok": False, "error": "Model manager not available"}, status_code=503)
    try:
        from ...db.session import async_session
        from ...world.world_model import WorldModel

        async with async_session() as session:
            wm = WorldModel()
            prediction = await wm.generate_forecast(session, entity, model_manager)

        if not prediction:
            return JSONResponse(
                {"ok": False, "error": "No entity data found or forecast generation failed"},
                status_code=404,
            )
        return JSONResponse({"ok": True, "entity": entity, "prediction": prediction})
    except Exception as exc:
        logger.exception("world_model.forecast_error", entity=entity)
        return JSONResponse({"ok": False, "error": str(exc)[:120]}, status_code=500)


@router.get("/api/predictions")
async def get_predictions(request: Request):
    try:
        from ...db.session import async_session
        from ...db.models import StatePrediction
        from datetime import datetime, timezone

        async with async_session() as session:
            rows = (await session.execute(
                select(StatePrediction)
                .where(
                    (StatePrediction.expires_at == None)  # noqa: E711
                    | (StatePrediction.expires_at > datetime.now(timezone.utc))
                )
                .order_by(StatePrediction.created_at.desc())
                .limit(20)
            )).scalars().all()

        return JSONResponse({
            "ok": True,
            "predictions": [
                {
                    "entity": r.entity,
                    "prediction_text": r.prediction_text,
                    "horizon": r.horizon,
                    "confidence": round(r.confidence, 2),
                    "model_used": r.model_used or "",
                    "created_at": r.created_at.isoformat() if r.created_at else "",
                }
                for r in rows
            ],
        })
    except Exception as exc:
        logger.exception("world_model.predictions_error")
        return JSONResponse({"ok": False, "predictions": [], "error": str(exc)[:120]})


@router.get("/api/temporal-insights")
async def get_temporal_insights(request: Request):
    try:
        from ...db.session import async_session
        from ...reasoning.temporal_reasoner import TemporalReasoner

        async with async_session() as session:
            reasoner = TemporalReasoner(max_insights=10)
            summaries = await reasoner.compute_all_trends(session, hours=24.0)

        return JSONResponse({
            "ok": True,
            "insights": [
                {
                    "entity": s.entity,
                    "current_value": s.current_value,
                    "previous_value": s.previous_value,
                    "change_pct": s.change_pct,
                    "trend": s.trend,
                    "observations": s.observations,
                    "time_window_hours": s.time_window_hours,
                }
                for s in summaries
            ],
        })
    except Exception as exc:
        logger.exception("world_model.insights_error")
        return JSONResponse({"ok": False, "insights": [], "error": str(exc)[:120]})
