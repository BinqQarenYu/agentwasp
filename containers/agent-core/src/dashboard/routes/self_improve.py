"""Self-Improve — review, apply or reject agent code proposals."""
from __future__ import annotations

import ast
import json
import os
import shutil
import time
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select, desc

router = APIRouter()
logger = structlog.get_logger()


@router.get("/", response_class=HTMLResponse)
async def self_improve_page(request: Request):
    redis_url = request.app.state.redis_url
    proposals = []

    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(redis_url, decode_responses=True)
        try:
            raw_props = await r.hgetall("self_improve:proposals")
            for pid, raw in raw_props.items():
                try:
                    p = json.loads(raw)
                    ts = p.get("created_at", 0)
                    proposals.append({
                        "id": p.get("id", pid),
                        "file": p.get("file", ""),
                        "change": (p.get("change") or "")[:600],
                        "diff": p.get("diff") or "",
                        "created_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "",
                        "ts": ts,
                    })
                except Exception:
                    logger.exception("self_improve_page.proposal_parse_error", pid=pid)
            proposals.sort(key=lambda x: x["ts"], reverse=True)
        finally:
            await r.aclose()
    except Exception:
        logger.exception("self_improve_page.proposals_load_error")

    # Applied history from audit log
    applied_history = []
    try:
        from ...db.models import AuditLog
        from ...db.session import async_session
        async with async_session() as session:
            result = await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "skill.self_improve"
                ).order_by(desc(AuditLog.timestamp)).limit(25)
            )
            for row in result.scalars().all():
                applied_history.append({
                    "timestamp": row.timestamp.strftime("%Y-%m-%d %H:%M") if row.timestamp else "",
                    "input": (row.input_summary or "")[:150],
                    "output": (row.output_summary or "")[:150],
                    "error": (row.error or "")[:100],
                    "ok": not bool(row.error),
                })
    except Exception:
        logger.exception("self_improve_page.history_load_error")

    return request.app.state.templates.TemplateResponse(request, "self_improve.html", {
        "proposals": proposals,
        "proposal_count": len(proposals),
        "applied_history": applied_history,
    })


@router.post("/api/{proposal_id}/reject")
async def reject_proposal(request: Request, proposal_id: str):
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(request.app.state.redis_url, decode_responses=True)
        try:
            deleted = await r.hdel("self_improve:proposals", proposal_id)
        finally:
            await r.aclose()
        if not deleted:
            return JSONResponse({"ok": False, "error": "Proposal not found"}, status_code=404)
        logger.info("self_improve.rejected", proposal_id=proposal_id)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e).splitlines()[0][:120]}, status_code=500)


@router.post("/api/{proposal_id}/apply")
async def apply_proposal(request: Request, proposal_id: str):
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(request.app.state.redis_url, decode_responses=True)
        try:
            raw = await r.hget("self_improve:proposals", proposal_id)
            if not raw:
                return JSONResponse({"ok": False, "error": "Proposal not found"}, status_code=404)
            proposal = json.loads(raw)
            file_path = proposal.get("file", "")
            content = proposal.get("diff", "")
            if not file_path or not content:
                return JSONResponse({"ok": False, "error": "Missing file or content"}, status_code=400)
            full = os.path.realpath(os.path.join("/app", file_path.lstrip("/")))
            if not full.startswith("/app"):
                return JSONResponse({"ok": False, "error": "Path traversal denied"}, status_code=400)

            # Syntax validation for Python files
            if full.endswith(".py"):
                try:
                    ast.parse(content)
                except SyntaxError as se:
                    logger.warning("self_improve.syntax_error", file=file_path,
                                   error=str(se)[:120], proposal_id=proposal_id)
                    return JSONResponse({"ok": False,
                                        "error": f"Syntax error — patch rejected: {str(se)[:120]}"},
                                       status_code=400)

            # Backup existing file before overwrite
            backup_path = None
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            safe_name = os.path.basename(full).replace("/", "_")
            backup_dir = "/data/src_patches"
            os.makedirs(backup_dir, exist_ok=True)
            if os.path.isfile(full):
                backup_path = os.path.join(backup_dir, f"backup_{ts}_{safe_name}")
                shutil.copy2(full, backup_path)
                logger.info("self_improve.backup_created", backup=backup_path)

            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(content)
            await r.hdel("self_improve:proposals", proposal_id)
            logger.info("self_improve.applied", proposal_id=proposal_id, file=file_path,
                        backup=backup_path)
            return JSONResponse({"ok": True, "file": file_path, "backup": backup_path})
        finally:
            await r.aclose()
    except Exception as e:
        logger.exception("self_improve.apply_error")
        return JSONResponse({"ok": False, "error": str(e).splitlines()[0][:120]}, status_code=500)
