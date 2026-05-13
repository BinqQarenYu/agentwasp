"""Knowledge Graph — entity/relation explorer with force visualization."""
from __future__ import annotations

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select, desc, func, delete

router = APIRouter()
logger = structlog.get_logger()


@router.get("/", response_class=HTMLResponse)
async def knowledge_graph_page(request: Request):
    nodes = []
    relations = []
    stats = {"node_count": 0, "relation_count": 0, "entity_types": {}}

    try:
        from ...db.models import KnowledgeNode, KnowledgeRelation
        from ...db.session import async_session
        async with async_session() as session:
            # Nodes
            node_result = await session.execute(
                select(KnowledgeNode).order_by(desc(KnowledgeNode.created_at)).limit(200)
            )
            node_rows = node_result.scalars().all()
            node_ids = {n.id for n in node_rows}
            for n in node_rows:
                nodes.append({
                    "id": n.id,
                    "name": n.name,
                    "entity_type": n.entity_type or "concept",
                    "description": (n.description or "")[:100],
                    "confidence": round(float(n.confidence or 0.5), 2),
                    "created_at": n.created_at.strftime("%Y-%m-%d %H:%M") if n.created_at else "",
                })
                t = n.entity_type or "concept"
                stats["entity_types"][t] = stats["entity_types"].get(t, 0) + 1

            # Relations (only between loaded nodes)
            rel_result = await session.execute(
                select(KnowledgeRelation).order_by(desc(KnowledgeRelation.created_at)).limit(500)
            )
            for r in rel_result.scalars().all():
                if r.from_node_id in node_ids and r.to_node_id in node_ids:
                    relations.append({
                        "id": r.id,
                        "from_id": r.from_node_id,
                        "to_id": r.to_node_id,
                        "relation_type": r.relation_type or "relates_to",
                        "confidence": round(float(r.confidence or 1.0), 2),
                    })

            stats["node_count"] = len(nodes)
            stats["relation_count"] = len(relations)

    except Exception:
        logger.exception("knowledge_graph_page.load_error")

    return request.app.state.templates.TemplateResponse(request, "knowledge_graph.html", {
        "nodes": nodes,
        "relations": relations,
        "stats": stats,
    })


@router.get("/api/data")
async def kg_data(request: Request):
    """JSON endpoint for live graph data."""
    try:
        from ...db.models import KnowledgeNode, KnowledgeRelation
        from ...db.session import async_session
        async with async_session() as session:
            node_result = await session.execute(
                select(KnowledgeNode).order_by(desc(KnowledgeNode.created_at)).limit(200)
            )
            nodes = []
            node_ids = set()
            for n in node_result.scalars().all():
                nodes.append({
                    "id": n.id, "name": n.name,
                    "entity_type": n.entity_type or "concept",
                    "description": (n.description or "")[:80],
                    "confidence": round(float(n.confidence or 0.5), 2),
                })
                node_ids.add(n.id)

            rel_result = await session.execute(select(KnowledgeRelation).limit(500))
            relations = []
            for r in rel_result.scalars().all():
                if r.from_node_id in node_ids and r.to_node_id in node_ids:
                    relations.append({
                        "id": r.id,
                        "from_id": r.from_node_id,
                        "to_id": r.to_node_id,
                        "relation_type": r.relation_type or "relates_to",
                    })
        return JSONResponse({"ok": True, "nodes": nodes, "relations": relations})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e).splitlines()[0][:120]}, status_code=500)


@router.get("/api/search")
async def kg_search(request: Request, q: str = ""):
    if not q or len(q) < 2:
        return JSONResponse({"ok": False, "error": "Query too short"})
    try:
        from ...db.models import KnowledgeNode
        from ...db.session import async_session
        async with async_session() as session:
            result = await session.execute(
                select(KnowledgeNode).where(
                    KnowledgeNode.name.ilike(f"%{q}%")
                ).order_by(desc(KnowledgeNode.created_at)).limit(30)
            )
            nodes = [{"id": n.id, "name": n.name, "entity_type": n.entity_type,
                      "description": (n.description or "")[:100],
                      "confidence": round(float(n.confidence or 0.5), 2)}
                     for n in result.scalars().all()]
        return JSONResponse({"ok": True, "nodes": nodes})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e).splitlines()[0][:120]}, status_code=500)


@router.delete("/api/node/{node_id}")
async def delete_node(request: Request, node_id: str):
    try:
        from ...db.models import KnowledgeNode, KnowledgeRelation
        from ...db.session import async_session
        async with async_session() as session:
            await session.execute(
                delete(KnowledgeRelation).where(
                    (KnowledgeRelation.from_node_id == node_id) |
                    (KnowledgeRelation.to_node_id == node_id)
                )
            )
            node = await session.get(KnowledgeNode, node_id)
            if not node:
                return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
            await session.delete(node)
            await session.commit()
        try:
            import redis.asyncio as aioredis
            r = aioredis.from_url(request.app.state.redis_url, decode_responses=True)
            await r.delete(f"kg:node:{node_id}")
            await r.aclose()
        except Exception:
            pass
        logger.info("kg.node_deleted", node_id=node_id)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e).splitlines()[0][:120]}, status_code=500)
