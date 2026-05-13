"""Audit log viewer routes."""

from datetime import datetime, timezone
from urllib.parse import urlencode

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import and_, func, or_, select, text

from ...db.models import AuditLog
from ...db.session import async_session

router = APIRouter()
logger = structlog.get_logger()

_PER_PAGE = 25


def _build_filters(stmt, event_type: str, source: str, errors_only: bool, search: str):
    if event_type:
        stmt = stmt.where(AuditLog.event_type == event_type)
    if source:
        stmt = stmt.where(AuditLog.source == source)
    if errors_only:
        stmt = stmt.where(AuditLog.error.isnot(None))
    if search:
        # Escape LIKE wildcards so literal % and _ in search are not treated as patterns
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        stmt = stmt.where(
            or_(
                AuditLog.input_summary.ilike(pattern, escape="\\"),
                AuditLog.output_summary.ilike(pattern, escape="\\"),
            )
        )
    return stmt


def _parse_cursor(cursor: str):
    """Parse 'isoformat|uuid' cursor string → (datetime, str) or (None, None)."""
    if not cursor:
        return None, None
    try:
        ts_str, uid = cursor.split("|", 1)
        return datetime.fromisoformat(ts_str), uid
    except Exception:
        return None, None


def _make_cursor(entry: AuditLog) -> str:
    return f"{entry.timestamp.isoformat()}|{entry.id}"


@router.get("/", response_class=HTMLResponse)
async def audit_page(
    request: Request,
    cursor: str = Query(default=""),        # keyset cursor: "ts_iso|id" of last item on prev page
    page: int = Query(default=1, ge=1),     # display counter only (not used for OFFSET)
    event_type: str = Query(default=""),
    source: str = Query(default=""),
    errors_only: bool = Query(default=False),
    search: str = Query(default=""),
):
    cursor_ts, cursor_id = _parse_cursor(cursor)
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    # Defaults — used if DB is unreachable
    entries: list = []
    has_more = False
    next_cursor = ""
    event_types: list = []
    sources: list = []
    event_type_counts: list = []
    stats_total = stats_errors = stats_today = stats_errors_today = stats_avg_lat = 0
    latest_ts = None
    hourly: list = [{"total": 0, "errors": 0} for _ in range(24)]

    try:
        async with async_session() as session:
            # Base filtered query (keyset — no OFFSET)
            base_stmt = select(AuditLog)
            base_stmt = _build_filters(base_stmt, event_type, source, errors_only, search)

            if cursor_ts is not None and cursor_id is not None:
                # Keyset: rows strictly before cursor (timestamp DESC, id DESC)
                base_stmt = base_stmt.where(
                    or_(
                        AuditLog.timestamp < cursor_ts,
                        and_(AuditLog.timestamp == cursor_ts, AuditLog.id < cursor_id),
                    )
                )

            # Fetch one extra row to detect whether another page exists
            rows = list((
                await session.execute(
                    base_stmt.order_by(AuditLog.timestamp.desc(), AuditLog.id.desc())
                    .limit(_PER_PAGE + 1)
                )
            ).scalars().all())

            has_more = len(rows) > _PER_PAGE
            entries = rows[:_PER_PAGE]

            # Build next-page cursor from last item in this page
            next_cursor = _make_cursor(entries[-1]) if (entries and has_more) else ""

            # Distinct event types for dropdown (skip empty strings like sources does)
            event_types = [
                row[0] for row in
                (await session.execute(
                    select(AuditLog.event_type).distinct().order_by(AuditLog.event_type)
                )).all()
                if row[0]
            ]

            # Distinct sources for dropdown
            sources = [
                row[0] for row in
                (await session.execute(
                    select(AuditLog.source).distinct().order_by(AuditLog.source)
                )).all()
                if row[0]
            ]

            # Event type counts for quick-filter pills (top 12)
            et_counts_raw = (await session.execute(
                select(AuditLog.event_type, func.count(AuditLog.id).label("cnt"))
                .group_by(AuditLog.event_type)
                .order_by(func.count(AuditLog.id).desc())
                .limit(12)
            )).all()
            event_type_counts = [(row[0], row[1]) for row in et_counts_raw]

            # ── Stats ──────────────────────────────────────────────────────────
            stats_total = (await session.execute(select(func.count(AuditLog.id)))).scalar_one()

            stats_errors = (await session.execute(
                select(func.count(AuditLog.id)).where(AuditLog.error.isnot(None))
            )).scalar_one()

            stats_today = (await session.execute(
                select(func.count(AuditLog.id)).where(AuditLog.timestamp >= today_start)
            )).scalar_one()

            stats_errors_today = (await session.execute(
                select(func.count(AuditLog.id)).where(
                    and_(AuditLog.timestamp >= today_start, AuditLog.error.isnot(None))
                )
            )).scalar_one()

            avg_lat_row = (await session.execute(
                select(func.avg(AuditLog.latency_ms)).where(
                    and_(AuditLog.latency_ms > 0, AuditLog.timestamp >= today_start)
                )
            )).scalar_one()
            stats_avg_lat = round(avg_lat_row or 0)

            # Latest entry timestamp (for live new-entries polling)
            latest_ts_row = (await session.execute(
                select(AuditLog.timestamp).order_by(AuditLog.timestamp.desc()).limit(1)
            )).scalar_one_or_none()
            latest_ts = latest_ts_row.isoformat() if latest_ts_row else None

            # ── 24h hourly activity chart ─────────────────────────────────────
            hourly_rows = (await session.execute(text("""
                SELECT
                    LEAST(23, GREATEST(0,
                        FLOOR(EXTRACT(EPOCH FROM (timestamp - :since)) / 3600)::int
                    )) AS bucket,
                    COUNT(*)         AS total,
                    COUNT(error)     AS errors
                FROM audit_log
                WHERE timestamp >= :since
                GROUP BY bucket ORDER BY bucket
            """), {"since": today_start})).mappings().all()

            hourly = [{"total": 0, "errors": 0} for _ in range(24)]
            for row in hourly_rows:
                b = max(0, min(23, int(row["bucket"])))
                hourly[b] = {"total": int(row["total"]), "errors": int(row["errors"])}

    except Exception:
        logger.exception("audit_page.db_error")

    # Build filter query string (no cursor/page — those are added per-link)
    qs_params: dict = {}
    if event_type:
        qs_params["event_type"] = event_type
    if source:
        qs_params["source"] = source
    if errors_only:
        qs_params["errors_only"] = "true"
    if search:
        qs_params["search"] = search
    filter_qs = urlencode(qs_params)

    return request.app.state.templates.TemplateResponse(request, "audit.html", {
        "entries": entries,
        "page": page,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "filter_qs": filter_qs,
        # kept for backwards compat with stats display
        "total": stats_total,
        "event_type": event_type,
        "event_types": event_types,
        "sources": sources,
        "current_source": source,
        "errors_only": errors_only,
        "search": search,
        "event_type_counts": event_type_counts,
        "stats": {
            "total":         stats_total,
            "errors":        stats_errors,
            "today":         stats_today,
            "errors_today":  stats_errors_today,
            "avg_lat":       stats_avg_lat,
            "sources_count": len(sources),
        },
        "hourly": hourly,
        "latest_ts": latest_ts,
        # Legacy — kept so old templates referencing these don't crash
        "pagination_qs": filter_qs,
        "total_pages": None,
    })


@router.get("/api/stats")
async def audit_api_stats(request: Request):
    """Live stats for auto-refresh."""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    try:
        async with async_session() as session:
            total = (await session.execute(select(func.count(AuditLog.id)))).scalar_one()
            errors = (await session.execute(
                select(func.count(AuditLog.id)).where(AuditLog.error.isnot(None))
            )).scalar_one()
            today = (await session.execute(
                select(func.count(AuditLog.id)).where(AuditLog.timestamp >= today_start)
            )).scalar_one()
            errors_today = (await session.execute(
                select(func.count(AuditLog.id)).where(
                    and_(AuditLog.timestamp >= today_start, AuditLog.error.isnot(None))
                )
            )).scalar_one()
            avg_lat_row = (await session.execute(
                select(func.avg(AuditLog.latency_ms)).where(
                    and_(AuditLog.latency_ms > 0, AuditLog.timestamp >= today_start)
                )
            )).scalar_one()
            latest_ts_row = (await session.execute(
                select(AuditLog.timestamp).order_by(AuditLog.timestamp.desc()).limit(1)
            )).scalar_one_or_none()

        return JSONResponse({
            "ok": True,
            "total":        total,
            "errors":       errors,
            "today":        today,
            "errors_today": errors_today,
            "avg_lat":      round(avg_lat_row or 0),
            "latest_ts":    latest_ts_row.isoformat() if latest_ts_row else None,
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)[:120]}, status_code=500)
