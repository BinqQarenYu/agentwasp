"""Subscriptions dashboard — RSS feeds and price alerts."""
from __future__ import annotations

import json
import time

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()
logger = structlog.get_logger()

SUBSCRIPTIONS_KEY = "subscriptions"


def _format_sub(sub_id: str, raw: str) -> dict | None:
    try:
        s = json.loads(raw)
        interval_s = s.get("interval_seconds", 1800)
        if interval_s >= 86400:
            interval_label = f"{interval_s // 86400}d"
        elif interval_s >= 3600:
            interval_label = f"{interval_s // 3600}h"
        else:
            interval_label = f"{interval_s // 60}m"

        last_check = s.get("last_check", 0)
        last_alert = s.get("last_alert", 0)
        created_at = s.get("created_at", 0)

        # Coerce thresholds to float so Jinja2 {:,.0f} format never crashes
        raw_above = s.get("above")
        raw_below = s.get("below")

        return {
            "id": sub_id,
            "type": s.get("type", "rss"),
            "name": s.get("name", sub_id),
            "status": s.get("status", "active"),
            "url": s.get("url", ""),
            "symbol": s.get("symbol", ""),
            "above": float(raw_above) if raw_above is not None else None,
            "below": float(raw_below) if raw_below is not None else None,
            "interval_label": interval_label,
            "interval_seconds": interval_s,
            "last_check": time.strftime("%Y-%m-%d %H:%M", time.localtime(last_check)) if last_check else "never",
            "last_alert": time.strftime("%Y-%m-%d %H:%M", time.localtime(last_alert)) if last_alert else "never",
            "created_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(created_at)) if created_at else "",
            "seen_count": len(s.get("seen_ids", [])),
        }
    except Exception:
        logger.exception("subscriptions._format_sub_error", sub_id=sub_id)
        return None


@router.get("/", response_class=HTMLResponse)
async def subscriptions_page(request: Request):
    subs = []
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(request.app.state.redis_url, decode_responses=True)
        try:
            all_subs = await r.hgetall(SUBSCRIPTIONS_KEY)
            for sub_id, raw in all_subs.items():
                sub = _format_sub(sub_id, raw)
                if sub:
                    subs.append(sub)
            subs.sort(key=lambda s: s["created_at"], reverse=True)
        finally:
            await r.aclose()
    except Exception:
        logger.exception("subscriptions_page.load_error")

    rss_subs = [s for s in subs if s["type"] == "rss"]
    price_subs = [s for s in subs if s["type"] == "price"]
    active_count = sum(1 for s in subs if s["status"] == "active")

    return request.app.state.templates.TemplateResponse(request, "subscriptions.html", {
        "subs": subs,
        "rss_subs": rss_subs,
        "price_subs": price_subs,
        "active_count": active_count,
        "total_count": len(subs),
    })


@router.post("/api/{sub_id}/pause")
async def pause_sub(request: Request, sub_id: str):
    return await _set_status(request, sub_id, "paused")


@router.post("/api/{sub_id}/resume")
async def resume_sub(request: Request, sub_id: str):
    return await _set_status(request, sub_id, "active")


async def _set_status(request: Request, sub_id: str, status: str):
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(request.app.state.redis_url, decode_responses=True)
        try:
            raw = await r.hget(SUBSCRIPTIONS_KEY, sub_id)
            if not raw:
                return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
            s = json.loads(raw)
            s["status"] = status
            await r.hset(SUBSCRIPTIONS_KEY, sub_id, json.dumps(s))
            logger.info("subscriptions.status_changed", sub_id=sub_id, status=status)
            return JSONResponse({"ok": True, "status": status})
        finally:
            await r.aclose()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e).splitlines()[0][:120]}, status_code=500)


@router.delete("/api/{sub_id}")
async def delete_sub(request: Request, sub_id: str):
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(request.app.state.redis_url, decode_responses=True)
        try:
            deleted = await r.hdel(SUBSCRIPTIONS_KEY, sub_id)
        finally:
            await r.aclose()
        if not deleted:
            return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
        logger.info("subscriptions.deleted", sub_id=sub_id)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e).splitlines()[0][:120]}, status_code=500)
