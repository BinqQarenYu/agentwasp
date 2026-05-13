"""Neural Brain Visualization — live graph of agent knowledge, skills, memory and goals."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select, text

from ...db.session import async_session
from ...db.models import KnowledgeNode, KnowledgeRelation

logger = structlog.get_logger()
router = APIRouter()

# Skill → epistemic domain mapping (mirrors integrity.py)
_SKILL_TO_DOMAIN: dict[str, str] = {
    "web_search": "web_scraping", "browser": "web_scraping",
    "fetch_url": "web_scraping", "scrape": "web_scraping",
    "python_exec": "programming", "shell": "programming",
    "http_request": "programming", "read_file": "programming",
    "write_file": "programming", "calculate": "data_analysis",
    "summarize": "data_analysis", "subscribe": "automation",
    "reminder": "automation", "skill_manager": "programming",
    "self_improve": "programming", "memory_search": "programming",
    "memory_store": "programming",
}


@router.get("/", response_class=HTMLResponse)
async def brain_page(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "brain.html", {})


@router.get("/data")
async def brain_data(request: Request):
    """Return real agent graph: KG nodes/edges + skills + epistemic + self-model + goals."""
    redis_url = request.app.state.redis_url

    nodes: list[dict] = []
    edges: list[dict] = []

    # ── Core node ────────────────────────────────────────────────────────────
    nodes.append({
        "id": "core",
        "label": "Agent Core",
        "category": "core",
        "size": 2.0,
        "detail": "Central agent intelligence — all systems connect here.",
        "meta": {},
    })

    # ── Load Redis data ───────────────────────────────────────────────────────
    self_model: dict = {}
    epistemic: dict = {}
    goals_raw: dict = {}
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(redis_url, decode_responses=True)
        try:
            sm_raw = await r.get("agent:self_model")
            if sm_raw:
                self_model = json.loads(sm_raw)
            ep_raw = await r.get("agent:epistemic")
            if ep_raw:
                epistemic = json.loads(ep_raw)
            goals_raw = await r.hgetall("goals") or {}
        finally:
            await r.aclose()
    except Exception as e:
        logger.warning("brain.redis_error", error=str(e)[:80])

    # ── Skills — from live registry (authoritative) merged with self-model rates
    skill_ids: set[str] = set()
    sm_rates = self_model.get("skill_success_rates", {})
    try:
        registry = request.app.state.skill_registry
        registry_defs = {d.name: d for d in (registry.list_all() or [])}
    except Exception:
        registry_defs = {}

    # Union: registry names + self-model rate names
    all_skill_names: set[str] = set(registry_defs.keys()) | set(sm_rates.keys())

    for skill_name in all_skill_names:
        stats = sm_rates.get(skill_name, {})
        success = stats.get("success", 0)
        failure = stats.get("failure", 0)
        total = success + failure
        rate = (success / total) if total > 0 else 0.5
        size = round(0.6 + rate * 0.9, 2)
        sid = f"skill:{skill_name}"
        skill_ids.add(sid)
        defn = registry_defs.get(skill_name)
        detail = f"Success rate: {rate:.0%} ({success}/{total} runs)" if total else (defn.description[:150] if defn else f"Registered skill: {skill_name}")
        nodes.append({
            "id": sid,
            "label": skill_name,
            "category": "skill",
            "size": size,
            "detail": detail,
            "meta": {"success_rate": round(rate, 3), "total": total},
        })
        edges.append({
            "source": "core",
            "target": sid,
            "strength": round(0.4 + rate * 0.6, 2),
            "relation": "uses",
        })

    # ── Self-model strengths ──────────────────────────────────────────────────
    for i, strength in enumerate(self_model.get("strengths", [])[:10]):
        sid = f"strength:{i}"
        nodes.append({
            "id": sid,
            "label": strength[:40],
            "category": "self_model",
            "size": 0.9,
            "detail": f"Identified strength: {strength}",
            "meta": {},
        })
        edges.append({"source": "core", "target": sid, "strength": 0.65, "relation": "strength"})

    # ── Epistemic domains ─────────────────────────────────────────────────────
    epistemic_ids: set[str] = set()
    for domain, confidence in epistemic.get("domain_confidence", {}).items():
        did = f"epistemic:{domain}"
        epistemic_ids.add(did)
        size = round(0.5 + confidence * 1.0, 2)
        nodes.append({
            "id": did,
            "label": domain.replace("_", " "),
            "category": "epistemic",
            "size": size,
            "detail": f"Domain confidence: {confidence:.0%}",
            "meta": {"confidence": round(confidence, 3)},
        })
        edges.append({
            "source": "core",
            "target": did,
            "strength": round(0.3 + confidence * 0.7, 2),
            "relation": "knows",
        })
        # Connect skills to their epistemic domain
        for skill_name, skill_domain in _SKILL_TO_DOMAIN.items():
            if skill_domain == domain:
                sk_id = f"skill:{skill_name}"
                if sk_id in skill_ids:
                    edges.append({
                        "source": sk_id,
                        "target": did,
                        "strength": 0.45,
                        "relation": "domain",
                    })

    # ── Active goals ──────────────────────────────────────────────────────────
    goal_count = 0
    for goal_id, goal_raw in goals_raw.items():
        if goal_count >= 12:
            break
        try:
            gdata = json.loads(goal_raw)
            state = gdata.get("state", "unknown")
            # Only show live goals — skip terminal states
            if state in ("completed", "failed", "cancelled", "archived"):
                continue
            objective = gdata.get("objective", "Unknown goal")
            priority = gdata.get("priority", 5)
            size = round(0.6 + (priority / 10) * 0.9, 2)
            gid = f"goal:{goal_id[:8]}"
            nodes.append({
                "id": gid,
                "label": (objective[:35] + "…") if len(objective) > 35 else objective,
                "category": "goal",
                "size": size,
                "detail": f"[{state}] Priority {priority}: {objective[:150]}",
                "meta": {"state": state, "priority": priority},
            })
            strength = 0.8 if state == "active" else 0.4
            edges.append({"source": "core", "target": gid, "strength": strength, "relation": "pursuing"})
            goal_count += 1
        except Exception:
            pass

    # ── Knowledge Graph (PostgreSQL) ──────────────────────────────────────────
    kg_id_map: dict[str, str] = {}  # db_id → node_id
    try:
        async with async_session() as session:
            # KG nodes (most recently updated, cap 100)
            kg_result = await session.execute(
                select(KnowledgeNode).order_by(KnowledgeNode.updated_at.desc()).limit(100)
            )
            kg_nodes_list = kg_result.scalars().all()

            for kn in kg_nodes_list:
                nid = f"kg:{kn.id[:8]}"
                kg_id_map[kn.id] = nid
                size = round(0.45 + kn.confidence * 0.75, 2)
                nodes.append({
                    "id": nid,
                    "label": kn.name[:35],
                    "category": "knowledge",
                    "size": size,
                    "detail": f"[{kn.entity_type}] {(kn.description or kn.name)[:150]}",
                    "meta": {"entity_type": kn.entity_type, "confidence": kn.confidence},
                })

            # KG relations
            if kg_id_map:
                all_kg_db_ids = list(kg_id_map.keys())
                kr_result = await session.execute(
                    select(KnowledgeRelation)
                    .where(KnowledgeRelation.from_node_id.in_(all_kg_db_ids))
                    .where(KnowledgeRelation.to_node_id.in_(all_kg_db_ids))
                    .limit(150)
                )
                for kr in kr_result.scalars().all():
                    src = kg_id_map.get(kr.from_node_id)
                    tgt = kg_id_map.get(kr.to_node_id)
                    if src and tgt and src != tgt:
                        edges.append({
                            "source": src,
                            "target": tgt,
                            "strength": round(max(0.2, kr.confidence * 0.8), 2),
                            "relation": kr.relation_type,
                        })

            # Procedural memories
            proc_result = await session.execute(text("""
                SELECT id, name, description, trigger_keywords, success_count
                FROM procedural_memory
                ORDER BY success_count DESC
                LIMIT 15
            """))
            for row in proc_result.fetchall():
                pid = f"mem:{str(row[0])[:8]}"
                use_count = row[4] or 0
                size = round(0.5 + min(use_count, 20) / 20 * 0.8, 2)
                nodes.append({
                    "id": pid,
                    "label": (row[1] or "procedure")[:35],
                    "category": "memory",
                    "size": size,
                    "detail": f"Procedure (×{use_count}): {(row[2] or '')[:150]}",
                    "meta": {"use_count": use_count, "keywords": row[3] or ""},
                })
                strength = round(0.3 + min(use_count, 20) / 20 * 0.6, 2)
                edges.append({"source": "core", "target": pid, "strength": strength, "relation": "remembers"})

    except Exception as e:
        logger.warning("brain.db_error", error=str(e)[:200])

    # ── Cross-connections ─────────────────────────────────────────────────────
    # Build a quick lookup of all node ids present
    _all_node_ids = {n["id"] for n in nodes}

    # KG nodes that have no edge at all → anchor to core (keeps graph connected)
    _kg_connected: set[str] = {e["target"] for e in edges if e["source"] == "core"}
    _kg_connected |= {e["source"] for e in edges if e["target"] == "core"}
    for nid in list(_all_node_ids):
        if nid.startswith("kg:") and nid not in _kg_connected:
            edges.append({"source": "core", "target": nid, "strength": 0.25, "relation": "knows"})

    # KG nodes → matching epistemic domain (entity_type keyword heuristic)
    _entity_to_domain: dict[str, str] = {
        "person": "social", "organization": "social", "company": "social",
        "technology": "programming", "software": "programming", "code": "programming",
        "price": "data_analysis", "number": "data_analysis", "metric": "data_analysis",
        "crypto": "data_analysis", "market": "data_analysis",
        "event": "automation", "task": "automation", "reminder": "automation",
        "webpage": "web_scraping", "url": "web_scraping", "website": "web_scraping",
    }
    for n in nodes:
        if n["category"] != "knowledge":
            continue
        etype = (n["meta"].get("entity_type") or "").lower()
        domain = _entity_to_domain.get(etype)
        if domain:
            did = f"epistemic:{domain}"
            if did in _all_node_ids:
                edges.append({"source": n["id"], "target": did, "strength": 0.3, "relation": "categorized_as"})

    # Memory procedures → epistemic domains (trigger_keywords matching)
    _kw_domain_map: dict[str, str] = {
        "web": "web_scraping", "scrape": "web_scraping", "browser": "web_scraping", "url": "web_scraping",
        "code": "programming", "python": "programming", "script": "programming", "file": "programming",
        "price": "data_analysis", "data": "data_analysis", "analyse": "data_analysis", "calculate": "data_analysis",
        "reminder": "automation", "schedule": "automation", "task": "automation",
    }
    for n in nodes:
        if n["category"] != "memory":
            continue
        raw_kws = n["meta"].get("keywords") or ""
        kws = (" ".join(raw_kws) if isinstance(raw_kws, list) else str(raw_kws)).lower()
        for kw, domain in _kw_domain_map.items():
            if kw in kws:
                did = f"epistemic:{domain}"
                if did in _all_node_ids:
                    edges.append({"source": n["id"], "target": did, "strength": 0.35, "relation": "applies"})
                    break  # one domain per procedure

    # Goals → epistemic domains (objective text keyword matching)
    _goal_kw_map: dict[str, str] = {
        "web": "web_scraping", "browse": "web_scraping", "scrape": "web_scraping",
        "code": "programming", "python": "programming", "script": "programming",
        "price": "data_analysis", "market": "data_analysis", "analyse": "data_analysis",
        "remind": "automation", "schedule": "automation", "monitor": "automation",
    }
    for n in nodes:
        if n["category"] != "goal":
            continue
        detail_lower = (n["detail"] or "").lower()
        for kw, domain in _goal_kw_map.items():
            if kw in detail_lower:
                did = f"epistemic:{domain}"
                if did in _all_node_ids:
                    edges.append({"source": n["id"], "target": did, "strength": 0.3, "relation": "requires"})
                    break

    # Cross-epistemic edges for related domains
    _related_domains: list[tuple[str, str]] = [
        ("programming", "data_analysis"),
        ("web_scraping", "programming"),
        ("automation", "programming"),
        ("data_analysis", "automation"),
    ]
    for d1, d2 in _related_domains:
        id1, id2 = f"epistemic:{d1}", f"epistemic:{d2}"
        if id1 in _all_node_ids and id2 in _all_node_ids:
            edges.append({"source": id1, "target": id2, "strength": 0.2, "relation": "related"})

    # ── Deduplicate edges ─────────────────────────────────────────────────────
    node_id_set = {n["id"] for n in nodes}
    seen_edges: set[tuple] = set()
    unique_edges = []
    for e in edges:
        if e["source"] not in node_id_set or e["target"] not in node_id_set:
            continue
        if e["source"] == e["target"]:
            continue
        key = (e["source"], e["target"])
        if key not in seen_edges:
            seen_edges.add(key)
            unique_edges.append(e)

    return JSONResponse({
        "nodes": nodes,
        "edges": unique_edges,
        "stats": {
            "total_nodes": len(nodes),
            "total_edges": len(unique_edges),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    })
