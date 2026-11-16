"""Dashboard route — Cognitive Blackboard & Causal Strategic Campaigns."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse

from ...db.session import async_session
from ...agent_manager.blackboard import CognitiveBlackboard, Hypothesis
from ...world.strategic_engine import StrategicCampaignEngine

router = APIRouter()
logger = structlog.get_logger()

# Module-level instances with lazy fallback
_blackboard: CognitiveBlackboard | None = None
_strategic_engine: StrategicCampaignEngine | None = None


def get_blackboard(redis_url: str = "") -> CognitiveBlackboard:
    global _blackboard
    if _blackboard is None:
        _blackboard = CognitiveBlackboard(redis_url=redis_url)
    elif redis_url and not _blackboard.redis_url:
        _blackboard.redis_url = redis_url
    return _blackboard


def get_strategic_engine(blackboard: CognitiveBlackboard) -> StrategicCampaignEngine:
    global _strategic_engine
    if _strategic_engine is None:
        _strategic_engine = StrategicCampaignEngine(blackboard=blackboard)
    return _strategic_engine


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def blackboard_page(request: Request):
    """Render the Cognitive Blackboard & Causal Horizon Cockpit."""
    redis_url = getattr(request.app.state, "redis_url", "")
    bb = get_blackboard(redis_url)
    engine = get_strategic_engine(bb)

    hypotheses = await bb.list_hypotheses(limit=50)
    campaigns = await engine.list_campaigns(limit=20)

    # If empty, seed a rich initial demonstration hypothesis so the operator immediately sees the mesh in action
    if not hypotheses:
        demo = await bb.post_hypothesis(
            author_bot="bot-09",  # Dr. Albert
            topic="causal_anomaly",
            statement="Detected divergence between codebase AST entropy (H_AST = 0.8451) and test execution velocity.",
            evidence=[
                "AST entropy stable at 0.8451 across 3,200 nodes",
                "Unit test runtime shifted from 1.1s to 3.3s (+200% latency)",
                "Redundant loop constructs detected in test mocks",
            ],
            confidence=0.82,
            metadata={"source": "Dr. Albert Empirical Forensics"},
        )
        await bb.submit_critique(
            hypothesis_id=demo.id,
            critic_bot="bot-10",  # Ares
            verdict="CHALLENGE",
            counter_argument="Latency shift may be an artifact of Windows process fork timing rather than algorithmic degradation.",
            confidence_adjustment=-0.10,
        )
        await bb.submit_critique(
            hypothesis_id=demo.id,
            critic_bot="root-liaison",  # Prometheus
            verdict="AFFIRM",
            counter_argument="Confirmed. Enclave telemetry verifies AST cache miss. Optimization campaign authorized.",
            confidence_adjustment=0.15,
        )
        await bb.synthesize_consensus(demo.id)
        hypotheses = await bb.list_hypotheses(limit=50)

    return request.app.state.templates.TemplateResponse(request, "blackboard.html", {
        "title": "Cognitive Blackboard & Strategic Horizon — Agent Wasp",
        "hypotheses": [h.to_dict() for h in hypotheses],
        "campaigns": [c.to_dict() for c in campaigns],
    })


@router.get("/api/hypotheses")
async def api_list_hypotheses(
    request: Request,
    status: str | None = Query(None),
    topic: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    """JSON API to fetch active hypotheses."""
    redis_url = getattr(request.app.state, "redis_url", "")
    bb = get_blackboard(redis_url)
    items = await bb.list_hypotheses(status=status, topic=topic, limit=limit)
    return JSONResponse({
        "ok": True,
        "count": len(items),
        "hypotheses": [h.to_dict() for h in items],
    })


@router.post("/api/hypotheses")
async def api_post_hypothesis(request: Request):
    """Post a new cognitive hypothesis (agent or operator steering)."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    author = data.get("author_bot", "human-operator")
    topic = data.get("topic", "general")
    statement = data.get("statement", "").strip()
    evidence = data.get("evidence", [])
    confidence = float(data.get("confidence", 0.75))
    metadata = data.get("metadata", {})

    if not statement:
        return JSONResponse({"ok": False, "error": "Statement is required"}, status_code=400)

    redis_url = getattr(request.app.state, "redis_url", "")
    bb = get_blackboard(redis_url)

    hyp = await bb.post_hypothesis(
        author_bot=author,
        topic=topic,
        statement=statement,
        evidence=evidence,
        confidence=confidence,
        metadata=metadata,
    )

    return JSONResponse({"ok": True, "hypothesis": hyp.to_dict()})


@router.post("/api/hypotheses/{hyp_id}/critique")
async def api_submit_critique(hyp_id: str, request: Request):
    """Submit a peer critique from an agent or human operator."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    critic = data.get("critic_bot", "human-operator")
    verdict = data.get("verdict", "CHALLENGE").upper()
    counter_arg = data.get("counter_argument", "").strip()
    conf_adj = float(data.get("confidence_adjustment", 0.0))

    if not counter_arg:
        return JSONResponse({"ok": False, "error": "counter_argument is required"}, status_code=400)

    redis_url = getattr(request.app.state, "redis_url", "")
    bb = get_blackboard(redis_url)

    critique = await bb.submit_critique(
        hypothesis_id=hyp_id,
        critic_bot=critic,
        verdict=verdict,
        counter_argument=counter_arg,
        confidence_adjustment=conf_adj,
    )

    if not critique:
        return JSONResponse({"ok": False, "error": "Hypothesis not found"}, status_code=404)

    return JSONResponse({"ok": True, "critique": critique.to_dict()})


@router.post("/api/hypotheses/{hyp_id}/bid")
async def api_submit_bid(hyp_id: str, request: Request):
    """Submit an agent capability bid to execute a hypothesis/campaign."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    bot_id = data.get("bot_id", "")
    fit = float(data.get("capability_fit", 0.8))
    strategy = data.get("proposed_strategy", "").strip()
    cost = int(data.get("estimated_cost", 1000))

    if not bot_id or not strategy:
        return JSONResponse({"ok": False, "error": "bot_id and proposed_strategy are required"}, status_code=400)

    redis_url = getattr(request.app.state, "redis_url", "")
    bb = get_blackboard(redis_url)

    bid = await bb.submit_bid(
        hypothesis_id=hyp_id,
        bot_id=bot_id,
        capability_fit=fit,
        proposed_strategy=strategy,
        estimated_cost=cost,
    )

    if not bid:
        return JSONResponse({"ok": False, "error": "Hypothesis not found"}, status_code=404)

    return JSONResponse({"ok": True, "bid": bid.to_dict()})


@router.post("/api/hypotheses/{hyp_id}/synthesize")
async def api_synthesize_consensus(hyp_id: str, request: Request):
    """Synthesize multi-agent critiques into a calibrated epistemic consensus."""
    redis_url = getattr(request.app.state, "redis_url", "")
    bb = get_blackboard(redis_url)

    verdict = await bb.synthesize_consensus(hyp_id)
    if not verdict:
        return JSONResponse({"ok": False, "error": "Hypothesis not found"}, status_code=404)

    hyp = await bb.get_hypothesis(hyp_id)
    return JSONResponse({
        "ok": True,
        "verdict": verdict,
        "hypothesis": hyp.to_dict() if hyp else None,
    })


@router.get("/api/campaigns")
async def api_list_campaigns(request: Request, limit: int = Query(20, ge=1, le=100)):
    """List active causal campaigns."""
    redis_url = getattr(request.app.state, "redis_url", "")
    bb = get_blackboard(redis_url)
    engine = get_strategic_engine(bb)
    campaigns = await engine.list_campaigns(limit=limit)
    return JSONResponse({
        "ok": True,
        "count": len(campaigns),
        "campaigns": [c.to_dict() for c in campaigns],
    })


@router.post("/api/campaigns/synthesize")
async def api_synthesize_campaign(request: Request):
    """Trigger causal anomaly scan across EntityStates and synthesize a strategic campaign."""
    redis_url = getattr(request.app.state, "redis_url", "")
    bb = get_blackboard(redis_url)
    engine = get_strategic_engine(bb)

    try:
        body = await request.json()
    except Exception:
        body = {}

    target_entity = body.get("entity", "")
    trend = body.get("trend", "volatile")
    change_pct = body.get("change_pct", -18.5)
    current_val = body.get("current_value", "critical")

    async with async_session() as session:
        # If no entity specified, check for live anomalies
        if not target_entity:
            anomalies = await engine.detect_causal_anomalies(session)
            if anomalies:
                top = anomalies[0]
                target_entity = top["entity"]
                trend = top["trend"]
                change_pct = top.get("change_pct")
                current_val = top.get("current_value", "")
            else:
                target_entity = "system_ast_entropy"
                trend = "volatile"
                change_pct = 16.4
                current_val = "0.8451"

        campaign = await engine.synthesize_campaign(
            entity=target_entity,
            trend=trend,
            change_pct=change_pct,
            current_value=str(current_val),
            session=session,
        )

    return JSONResponse({
        "ok": True,
        "campaign": campaign.to_dict(),
        "blackboard_hypothesis_posted": True,
    })
