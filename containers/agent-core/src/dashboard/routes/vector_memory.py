"""Dashboard route — Vector Semantic Memory inspector."""
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
async def vector_memory_page(request: Request):
    from ...db.session import async_session
    from ...db.models import MemoryEmbedding
    from ...config import settings
    from datetime import datetime, timezone, timedelta

    stats = {
        "total": 0,
        "by_type": {},
        "by_model": {},
        "semantic_count": 0,
        "hash_count": 0,
        "semantic_pct": 0,
        "provider": getattr(settings, "embedding_provider", "ollama"),
        "embed_model": getattr(settings, "vector_embed_model", "nomic-embed-text"),
        "enabled": getattr(settings, "vector_memory_enabled", False),
        "recent": [],
        "growth": [],
    }
    try:
        now = datetime.now(timezone.utc)
        async with async_session() as session:
            stats["total"] = (
                await session.execute(select(func.count()).select_from(MemoryEmbedding))
            ).scalar() or 0

            by_type = (await session.execute(
                select(MemoryEmbedding.source_type, func.count().label("n"))
                .group_by(MemoryEmbedding.source_type)
            )).all()
            stats["by_type"] = {r.source_type: r.n for r in by_type}

            by_model = (await session.execute(
                select(MemoryEmbedding.embed_model, func.count().label("n"))
                .group_by(MemoryEmbedding.embed_model)
            )).all()
            stats["by_model"] = {r.embed_model: r.n for r in by_model}

            # Semantic vs hash split
            hash_count = sum(n for m, n in stats["by_model"].items() if "hash" in m.lower())
            semantic_count = stats["total"] - hash_count
            stats["hash_count"] = hash_count
            stats["semantic_count"] = semantic_count
            stats["semantic_pct"] = round(semantic_count / stats["total"] * 100) if stats["total"] > 0 else 0

            # Recent embeddings (last 20)
            recent_rows = (await session.execute(
                select(MemoryEmbedding).order_by(MemoryEmbedding.created_at.desc()).limit(20)
            )).scalars().all()
            stats["recent"] = [
                {
                    "source_id": r.source_id[:8],
                    "source_type": r.source_type,
                    "embed_model": r.embed_model,
                    "is_semantic": "hash" not in r.embed_model.lower(),
                    "preview": (r.content_preview or "")[:100],
                    "created_at": r.created_at.isoformat() if r.created_at else "",
                }
                for r in recent_rows
            ]

            # Weekly growth (last 8 weeks)
            growth = []
            for w in range(7, -1, -1):
                week_start = now - timedelta(weeks=w + 1)
                week_end = now - timedelta(weeks=w)
                n = (await session.execute(
                    select(func.count()).select_from(MemoryEmbedding).where(
                        MemoryEmbedding.created_at >= week_start,
                        MemoryEmbedding.created_at < week_end,
                    )
                )).scalar() or 0
                label = week_end.strftime("%-d %b") if w > 0 else "now"
                growth.append({"label": label, "count": n, "week_ago": w})
            stats["growth"] = growth

    except Exception:
        logger.exception("vector_memory_page.load_error")

    return request.app.state.templates.TemplateResponse(
        request, "vector_memory.html", {"stats": stats}
    )


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

@router.get("/api/stats")
async def vector_memory_stats(request: Request):
    """Return embedding statistics — used by auto-refresh."""
    from ...db.session import async_session
    from ...db.models import MemoryEmbedding
    from ...config import settings
    from datetime import datetime, timezone, timedelta
    try:
        now = datetime.now(timezone.utc)
        async with async_session() as session:
            total = (await session.execute(
                select(func.count()).select_from(MemoryEmbedding)
            )).scalar() or 0

            by_type = (await session.execute(
                select(MemoryEmbedding.source_type, func.count().label("n"))
                .group_by(MemoryEmbedding.source_type)
            )).all()

            by_model = (await session.execute(
                select(MemoryEmbedding.embed_model, func.count().label("n"))
                .group_by(MemoryEmbedding.embed_model)
            )).all()
            by_model_dict = {r.embed_model: r.n for r in by_model}

            hash_count = sum(n for m, n in by_model_dict.items() if "hash" in m.lower())
            semantic_count = total - hash_count
            semantic_pct = round(semantic_count / total * 100) if total > 0 else 0

            recent_rows = (await session.execute(
                select(MemoryEmbedding).order_by(MemoryEmbedding.created_at.desc()).limit(20)
            )).scalars().all()
            recent = [
                {
                    "source_id": r.source_id[:8],
                    "source_type": r.source_type,
                    "embed_model": r.embed_model,
                    "is_semantic": "hash" not in r.embed_model.lower(),
                    "preview": (r.content_preview or "")[:100],
                    "created_at": r.created_at.isoformat() if r.created_at else "",
                }
                for r in recent_rows
            ]

            growth = []
            for w in range(7, -1, -1):
                week_start = now - timedelta(weeks=w + 1)
                week_end = now - timedelta(weeks=w)
                n = (await session.execute(
                    select(func.count()).select_from(MemoryEmbedding).where(
                        MemoryEmbedding.created_at >= week_start,
                        MemoryEmbedding.created_at < week_end,
                    )
                )).scalar() or 0
                label = week_end.strftime("%-d %b") if w > 0 else "now"
                growth.append({"label": label, "count": n, "week_ago": w})

        return JSONResponse({
            "ok": True,
            "total": total,
            "by_type": {r.source_type: r.n for r in by_type},
            "by_model": by_model_dict,
            "semantic_count": semantic_count,
            "hash_count": hash_count,
            "semantic_pct": semantic_pct,
            "provider": getattr(settings, "embedding_provider", "ollama"),
            "embed_model": getattr(settings, "vector_embed_model", "nomic-embed-text"),
            "enabled": getattr(settings, "vector_memory_enabled", False),
            "recent": recent,
            "growth": growth,
        })
    except Exception as exc:
        logger.exception("vector_memory.stats_error")
        return JSONResponse({"ok": False, "total": 0, "error": str(exc)[:120]})


@router.post("/api/search")
async def vector_memory_search(request: Request):
    """Semantic search over indexed memories."""
    from ...config import settings
    import time
    try:
        body = await request.json()
        query = (body.get("query") or "").strip()
        source_type = (body.get("source_type") or "").strip() or None
        if not query:
            return JSONResponse({"ok": False, "results": [], "error": "Query required"})

        from ...db.session import async_session
        from ...memory.vector_memory import semantic_search
        from ...memory.embeddings import create_provider

        t0 = time.monotonic()
        async with async_session() as session:
            results = await semantic_search(
                session=session,
                query=query,
                provider=create_provider(settings),
                source_type=source_type,
                top_k=getattr(settings, "vector_top_k", 8),
            )
        latency_ms = round((time.monotonic() - t0) * 1000)

        return JSONResponse({
            "ok": True,
            "results": results,
            "latency_ms": latency_ms,
            "query": query,
        })
    except Exception as exc:
        logger.exception("vector_memory.search_error")
        return JSONResponse({"ok": False, "results": [], "error": str(exc)[:120]})


@router.post("/api/index-trigger")
async def trigger_index(request: Request):
    """Manually trigger the vector index job."""
    try:
        scheduler = getattr(request.app.state, "scheduler", None)
        if not scheduler:
            return JSONResponse({"ok": False, "error": "Scheduler not available"}, status_code=503)
        result = await scheduler.trigger("vector_index")
        return JSONResponse({"ok": True, "result": result})
    except Exception as exc:
        logger.exception("vector_memory.index_trigger_error")
        return JSONResponse({"ok": False, "error": str(exc)[:120]}, status_code=500)
