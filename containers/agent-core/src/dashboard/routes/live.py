"""Live Feed SSE — streams audit log events in real time."""

import asyncio
import json
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import select

from ...db.models import AuditLog
from ...db.session import async_session

router = APIRouter()
logger = structlog.get_logger()


@router.get("/", response_class=HTMLResponse)
async def live_page(request: Request):
    # Collect distinct event_types and sources for filter dropdowns
    event_types = []
    sources = []
    try:
        async with async_session() as session:
            event_types = [
                row[0] for row in
                (await session.execute(
                    select(AuditLog.event_type).distinct().order_by(AuditLog.event_type)
                )).all()
                if row[0]
            ]
            sources = [
                row[0] for row in
                (await session.execute(
                    select(AuditLog.source).distinct().order_by(AuditLog.source)
                )).all()
                if row[0]
            ]
    except Exception:
        logger.exception("live_page.db_error")

    return request.app.state.templates.TemplateResponse(request, "live.html", {
        "event_types": event_types,
        "sources": sources,
    })


@router.get("/stream")
async def live_stream(
    request: Request,
    event_type: str = Query(default=""),
    source: str = Query(default=""),
    errors_only: bool = Query(default=False),
):
    """SSE endpoint polling audit_log every 2 seconds for new events."""

    async def generator():
        cursor = datetime.now(timezone.utc)
        yield 'data: {"type":"connected"}\n\n'

        try:
            while True:
                try:
                    async with async_session() as session:
                        stmt = (
                            select(AuditLog)
                            .where(AuditLog.timestamp > cursor)
                            .order_by(AuditLog.timestamp.asc())
                            .limit(50)
                        )
                        if event_type:
                            stmt = stmt.where(AuditLog.event_type == event_type)
                        if source:
                            stmt = stmt.where(AuditLog.source == source)
                        if errors_only:
                            stmt = stmt.where(AuditLog.error.isnot(None))

                        entries = list((await session.execute(stmt)).scalars().all())

                    for entry in entries:
                        payload = {
                            "id": str(entry.id),
                            "ts": entry.timestamp.isoformat(),
                            "event_type": entry.event_type or "",
                            "source": entry.source or "",
                            "action": (entry.action or "")[:100],
                            "input": (entry.input_summary or "")[:120],
                            "latency_ms": entry.latency_ms or 0,
                            "has_error": entry.error is not None,
                            "error": (entry.error or "")[:120],
                        }
                        yield f"data: {json.dumps(payload)}\n\n"
                        cursor = entry.timestamp

                except Exception:
                    logger.exception("live_stream.poll_error")
                    yield 'data: {"type":"poll_error"}\n\n'

                # Keep-alive comment
                yield ": keep-alive\n\n"
                await asyncio.sleep(2.0)

        except asyncio.CancelledError:
            pass  # Client disconnected

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
