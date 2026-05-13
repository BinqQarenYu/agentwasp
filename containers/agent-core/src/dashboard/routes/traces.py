"""Decision trace viewer.

Renders the last 200 DecisionTrace records persisted by the central policy
to Redis (`decision_trace:index` + `decision_trace:{request_id}`).

Operator workflow: "Why did WASP do X?" → open /traces → filter by chat or
path → click the row → see exactly which guards fired and why.
"""

import json
from typing import Any

import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from ...config import settings

router = APIRouter()
logger = structlog.get_logger()
templates = Jinja2Templates(directory="src/dashboard/templates")


@router.get("/", response_class=HTMLResponse)
async def traces_page(
    request: Request,
    path: str = Query(default=""),
    chat_id: str = Query(default=""),
    has_guards: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=200),
):
    """List recent decision traces with optional filters."""
    traces: list[dict[str, Any]] = []
    total = 0
    try:
        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        try:
            ids = await r.lrange("decision_trace:index", 0, 199)
            total = await r.llen("decision_trace:index")
            for tid in ids[:200]:
                raw = await r.get(f"decision_trace:{tid}")
                if not raw:
                    continue
                try:
                    t = json.loads(raw)
                except Exception:
                    continue
                # Apply filters
                if path and t.get("path") != path:
                    continue
                if chat_id and chat_id not in (t.get("chat_id") or ""):
                    continue
                if has_guards and not (t.get("guard_actions") or []):
                    continue
                traces.append(t)
                if len(traces) >= limit:
                    break
        finally:
            await r.aclose()
    except Exception:
        logger.exception("traces.list_failed")

    # Distinct paths for the filter dropdown
    distinct_paths = sorted({t.get("path", "") for t in traces if t.get("path")})

    return templates.TemplateResponse(
        request,
        "traces.html",
        {
            "traces": traces,
            "total": total,
            "filter_path": path,
            "filter_chat_id": chat_id,
            "filter_has_guards": has_guards,
            "distinct_paths": distinct_paths,
        },
    )


@router.get("/api/{request_id}")
async def trace_detail(request_id: str):
    """Return the full JSON for one trace."""
    try:
        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        try:
            raw = await r.get(f"decision_trace:{request_id}")
        finally:
            await r.aclose()
        if not raw:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return JSONResponse(json.loads(raw))
    except Exception as e:
        return JSONResponse({"error": str(e)[:120]}, status_code=500)
