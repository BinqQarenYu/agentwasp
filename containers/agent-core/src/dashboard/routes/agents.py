"""Multi-Agent Orchestration — dashboard routes.

Endpoints:
  GET  /agents/                      → Agent list page
  GET  /agents/{agent_id}            → Agent detail page
  GET  /agents/api/status            → All agents snapshot (JSON, auto-refresh)
  GET  /agents/api/{id}              → Single agent data (JSON, auto-refresh)
  POST /agents/api/create            → Create agent
  POST /agents/api/{id}/pause        → Pause agent
  POST /agents/api/{id}/resume       → Resume agent
  POST /agents/api/{id}/archive      → Archive agent
  POST /agents/api/{id}/delete       → Hard-delete agent + goals
  POST /agents/api/{id}/start        → Create initial goal from identity_prompt
  GET  /agents/api/{id}/messages     → Recent messages
  POST /agents/api/{id}/send_message → Assign a goal to another agent
  POST /agents/api/{id}/spawn_subagent → Spawn Depth-1 Ephemeral Subagent
  POST /agents/api/{id}/reap_subagent  → Reap & archive Ephemeral Subagent
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()
logger = structlog.get_logger()


def _ao(request: Request):
    return getattr(request.app.state, "agent_orchestrator", None)


def _fmt_agent(agent) -> dict:
    """Convert Agent object to template-friendly dict."""
    budget = getattr(agent, "cognitive_budget", None)
    budget_pct_planning = 0.0
    budget_used_planning = 0
    budget_max_planning = 0
    budget_used_steps = 0
    budget_max_steps = 0
    budget_pct_steps = 0.0
    budget_exceeded = False
    if budget:
        budget_used_planning = getattr(budget, "tokens_used_planning", 0)
        budget_max_planning = getattr(budget, "max_tokens_planning", 0)
        if budget_max_planning > 0:
            budget_pct_planning = min(
                100.0,
                budget_used_planning / budget_max_planning * 100
            )
        budget_used_steps = getattr(budget, "steps_executed", 0)
        budget_max_steps = getattr(budget, "max_steps", 0)
        if budget_max_steps > 0:
            budget_pct_steps = min(100.0, budget_used_steps / budget_max_steps * 100)
        budget_exceeded = getattr(budget, "budget_exceeded", False)

    autonomy = getattr(agent, "autonomy_mode", "semi")
    autonomy_val = autonomy.value if hasattr(autonomy, "value") else str(autonomy)
    status_obj = getattr(agent, "status", "idle")
    status_val = status_obj.value if hasattr(status_obj, "value") else str(status_obj)
    active_goals = getattr(agent, "active_goal_ids", []) or []
    intent_obj = getattr(agent, "intent", None)
    intent_desc = (intent_obj.description[:120] if intent_obj and getattr(intent_obj, "description", None) else "")

    can_spawn = True
    if hasattr(agent, "can_spawn_subagents"):
        try:
            can_spawn = agent.can_spawn_subagents()
        except Exception:
            can_spawn = (getattr(agent, "depth", 0) == 0)
    else:
        can_spawn = (getattr(agent, "depth", 0) == 0)

    return {
        "id": agent.id,
        "name": agent.name,
        "description": getattr(agent, "description", ""),
        "model_provider": getattr(agent, "model_provider", ""),
        "model_name": getattr(agent, "model_name", ""),
        "identity_prompt": getattr(agent, "identity_prompt", ""),
        "autonomy_mode": autonomy_val,
        "allowed_capabilities": getattr(agent, "allowed_capabilities", []) or [],
        "memory_namespace": getattr(agent, "effective_namespace", getattr(agent, "memory_namespace", agent.id)),
        "status": status_val,
        "created_at": agent.created_at[:19] if getattr(agent, "created_at", None) else "",
        "updated_at": agent.updated_at[:19] if getattr(agent, "updated_at", None) else "",
        "active_goal_count": len(active_goals),
        "active_goal_ids": active_goals,
        "budget_used_planning": budget_used_planning,
        "budget_max_planning": budget_max_planning,
        "budget_pct_planning": round(budget_pct_planning, 1),
        "budget_used_steps": budget_used_steps,
        "budget_max_steps": budget_max_steps,
        "budget_pct_steps": round(budget_pct_steps, 1),
        "budget_exceeded": budget_exceeded,
        "intent_description": intent_desc,
        "depth": getattr(agent, "depth", 0),
        "parent_agent_id": getattr(agent, "parent_agent_id", None),
        "is_ephemeral": getattr(agent, "is_ephemeral", False),
        "spawned_subagents": getattr(agent, "spawned_subagents", []) or [],
        "max_subagents": getattr(agent, "max_subagents", 3),
        "task_timeout_seconds": getattr(agent, "task_timeout_seconds", 180),
        "can_spawn_subagents": can_spawn,
        "metadata": getattr(agent, "metadata", {}) or {},
    }


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
async def agents_list(request: Request):
    ao = _ao(request)
    agents_data = []
    if ao:
        try:
            agents = await ao.list_agents()
            agents_data = [_fmt_agent(a) for a in agents]
        except Exception:
            logger.exception("agents_route.list_error")

    status_counts = {"idle": 0, "running": 0, "paused": 0, "archived": 0}
    for a in agents_data:
        s = a.get("status", "idle")
        if s in status_counts:
            status_counts[s] += 1

    total = len(agents_data)
    stats = {
        "total": total,
        "running": status_counts["running"],
        "paused": status_counts["paused"],
        "idle": status_counts["idle"],
        "archived": status_counts["archived"],
    }

    return request.app.state.templates.TemplateResponse(
        request, "agents.html",
        {
            "agents": agents_data,
            "status_counts": status_counts,
            "stats": stats,
            "ao_enabled": ao is not None,
        },
    )


@router.get("/{agent_id}", response_class=HTMLResponse)
async def agent_detail(request: Request, agent_id: str):
    ao = _ao(request)
    agent_data = None
    messages = []

    if ao:
        agent = await ao.get_agent(agent_id)
        if agent:
            agent_data = _fmt_agent(agent)
            raw_msgs = await ao.get_messages(agent_id, limit=30)
            messages = [
                {
                    "id": str(m.id),
                    "from_agent_id": m.from_agent_id,
                    "content": m.content,
                    "message_type": m.message_type,
                    "created_at": m.created_at.isoformat()[:19] if m.created_at else "",
                    "read": m.read_at is not None,
                }
                for m in raw_msgs
            ]

    return request.app.state.templates.TemplateResponse(
        request, "agent_detail.html",
        {
            "agent": agent_data,
            "messages": messages,
            "agent_id": agent_id,
        },
    )


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

@router.post("/api/create")
async def agent_create(request: Request):
    ao = _ao(request)
    if not ao:
        return JSONResponse({"ok": False, "error": "Agent orchestrator not available"}, status_code=503)
    try:
        body = await request.json()
        name = (body.get("name") or "").strip()
        if not name:
            return JSONResponse({"ok": False, "error": "name is required"}, status_code=400)

        capabilities_raw = body.get("allowed_capabilities", [])
        if isinstance(capabilities_raw, str):
            capabilities_raw = [c.strip() for c in capabilities_raw.split(",") if c.strip()]

        identity_prompt = body.get("identity_prompt", "")
        agent = await ao.create_agent(
            name=name,
            description=body.get("description", ""),
            model_provider=body.get("model_provider", ""),
            model_name=body.get("model_name", ""),
            identity_prompt=identity_prompt,
            autonomy_mode=body.get("autonomy_mode", "full"),
            allowed_capabilities=capabilities_raw,
            memory_namespace=body.get("memory_namespace", ""),
        )

        # Auto-start initial goal from identity_prompt so agent begins immediately
        if identity_prompt.strip():
            try:
                await ao.create_agent_goal(agent.id, objective=identity_prompt)
            except Exception:
                logger.exception("agents_route.auto_start_failed", agent_id=agent.id)

        return JSONResponse({"ok": True, "agent": _fmt_agent(agent)})
    except Exception as e:
        logger.exception("agents_route.create_error")
        return JSONResponse({"ok": False, "error": str(e).splitlines()[0][:120]}, status_code=500)


@router.post("/api/{agent_id}/pause")
async def agent_pause(request: Request, agent_id: str):
    ao = _ao(request)
    if not ao:
        return JSONResponse({"ok": False, "error": "Agent orchestrator not available"}, status_code=503)
    ok = await ao.pause_agent(agent_id)
    if not ok:
        return JSONResponse({"ok": False, "error": "Agent not found or already archived"}, status_code=404)
    return JSONResponse({"ok": True})


@router.post("/api/{agent_id}/resume")
async def agent_resume(request: Request, agent_id: str):
    ao = _ao(request)
    if not ao:
        return JSONResponse({"ok": False, "error": "Agent orchestrator not available"}, status_code=503)
    ok = await ao.resume_agent(agent_id)
    if not ok:
        return JSONResponse({"ok": False, "error": "Agent not found or archived"}, status_code=404)
    return JSONResponse({"ok": True})


@router.post("/api/{agent_id}/archive")
async def agent_archive(request: Request, agent_id: str):
    ao = _ao(request)
    if not ao:
        return JSONResponse({"ok": False, "error": "Agent orchestrator not available"}, status_code=503)
    ok = await ao.archive_agent(agent_id)
    if not ok:
        return JSONResponse({"ok": False, "error": "Agent not found"}, status_code=404)
    return JSONResponse({"ok": True})


@router.get("/api/{agent_id}/messages")
async def agent_messages(request: Request, agent_id: str):
    ao = _ao(request)
    if not ao:
        return JSONResponse({"ok": False, "error": "Agent orchestrator not available"}, status_code=503)
    msgs = await ao.get_messages(agent_id, limit=50)
    return JSONResponse({
        "ok": True,
        "messages": [
            {
                "id": str(m.id),
                "from_agent_id": m.from_agent_id,
                "content": m.content,
                "message_type": m.message_type,
                "created_at": m.created_at.isoformat()[:19] if m.created_at else "",
                "read": m.read_at is not None,
            }
            for m in msgs
        ],
    })


@router.post("/api/{agent_id}/send_message")
async def agent_send_message(request: Request, agent_id: str):
    """Send an instruction to an agent by creating a goal for it."""
    ao = _ao(request)
    if not ao:
        return JSONResponse({"ok": False, "error": "Agent orchestrator not available"}, status_code=503)
    try:
        body = await request.json()
        content = (body.get("content") or body.get("message") or "").strip()
        target_id = (body.get("to_agent_id") or agent_id).strip()
        if not content:
            return JSONResponse({"ok": False, "error": "content or message is required"}, status_code=400)

        goal = await ao.create_agent_goal(target_id, objective=content)
        return JSONResponse({"ok": True, "goal_id": goal.id, "agent_id": target_id})
    except Exception as e:
        logger.exception("agents_route.send_message_error")
        return JSONResponse({"ok": False, "error": str(e).splitlines()[0][:120]}, status_code=500)


@router.post("/api/{agent_id}/start")
async def agent_start(request: Request, agent_id: str):
    """Create an initial goal from the agent's identity_prompt and start it."""
    ao = _ao(request)
    if not ao:
        return JSONResponse({"ok": False, "error": "Agent orchestrator not available"}, status_code=503)
    try:
        body = await request.json()
        agent = await ao.get_agent(agent_id)
        if not agent:
            return JSONResponse({"ok": False, "error": "Agent not found"}, status_code=404)
        objective = (body.get("objective") or agent.identity_prompt or agent.description or "").strip()
        if not objective:
            return JSONResponse({"ok": False, "error": "No objective provided and agent has no identity_prompt"}, status_code=400)
        goal = await ao.create_agent_goal(agent_id, objective=objective)
        return JSONResponse({"ok": True, "goal_id": goal.id})
    except Exception as e:
        logger.exception("agents_route.start_error")
        return JSONResponse({"ok": False, "error": str(e).splitlines()[0][:120]}, status_code=500)


@router.get("/api/status")
async def agents_status(request: Request):
    """JSON snapshot of all agents — used by auto-refresh."""
    ao = _ao(request)
    agents_data = []
    if ao:
        try:
            agents = await ao.list_agents()
            agents_data = [_fmt_agent(a) for a in agents]
        except Exception:
            logger.exception("agents_route.status_error")

    status_counts = {"idle": 0, "running": 0, "paused": 0, "archived": 0}
    for a in agents_data:
        s = a.get("status", "idle")
        if s in status_counts:
            status_counts[s] += 1

    return JSONResponse({
        "agents": agents_data,
        "status_counts": status_counts,
        "total": len(agents_data),
    })


@router.get("/api/{agent_id}")
async def agent_get(request: Request, agent_id: str):
    """Single agent snapshot as JSON — used by detail page auto-refresh."""
    ao = _ao(request)
    if not ao:
        return JSONResponse({"ok": False, "error": "Agent orchestrator not available"}, status_code=503)
    agent = await ao.get_agent(agent_id)
    if not agent:
        return JSONResponse({"ok": False, "error": "Agent not found"}, status_code=404)
    return JSONResponse({"ok": True, "agent": _fmt_agent(agent)})


@router.post("/api/{agent_id}/spawn_subagent")
async def agent_spawn_subagent(request: Request, agent_id: str):
    """Spawns an Ephemeral Depth-1 Subagent for specialized workloads."""
    ao = _ao(request)
    if not ao:
        return JSONResponse({"ok": False, "error": "Agent orchestrator not available"}, status_code=503)
    try:
        parent = await ao.get_agent(agent_id)
        if not parent:
            return JSONResponse({"ok": False, "error": f"Parent agent '{agent_id}' not found"}, status_code=404)

        body = await request.json()
        role = (body.get("role") or "").strip()
        task_objective = (body.get("task_objective") or body.get("objective") or "").strip()
        if not role:
            return JSONResponse({"ok": False, "error": "role is required"}, status_code=400)
        if not task_objective:
            return JSONResponse({"ok": False, "error": "task_objective is required"}, status_code=400)

        timeout_seconds = int(body.get("timeout_seconds") or 180)
        token_budget = int(body.get("token_budget") or 2000)
        model_provider = (body.get("model_provider") or "").strip()
        model_name = (body.get("model_name") or "").strip()

        # Rule 1: Depth-1 Boundary
        if getattr(parent, "depth", 0) > 0:
            return JSONResponse({
                "ok": False,
                "error": f"Depth limit exceeded: Agent '{parent.name}' (depth={parent.depth}) is a subagent and cannot spawn nested subagents."
            }, status_code=400)

        # Rule 2: Concurrency leash
        spawned = getattr(parent, "spawned_subagents", []) or []
        max_sub = getattr(parent, "max_subagents", 3)
        if len(spawned) >= max_sub:
            return JSONResponse({
                "ok": False,
                "error": f"Concurrency limit exceeded: Agent '{parent.name}' has reached its maximum of {max_sub} active subagents."
            }, status_code=400)

        # Rule 3: Ollama hardware safety on host CPU
        if model_provider.lower() == "ollama" and model_name:
            if model_name.lower().strip() not in {"llama3.2:latest", "llama3.2", "nemotron-mini:latest", "nemotron-mini"}:
                model_name = "llama3.2:latest"

        # Instantiate or fetch genesis
        try:
            from ...agent_manager.genesis import SubagentGenesis
        except (ImportError, ValueError):
            try:
                from src.agent_manager.genesis import SubagentGenesis
            except (ImportError, ValueError):
                from agent_manager.genesis import SubagentGenesis

        redis_url = getattr(request.app.state, "redis_url", "redis://localhost:6379/0")
        redis_client = getattr(request.app.state, "redis_client", None)
        genesis = SubagentGenesis(redis_url=redis_url, redis_client=redis_client)

        subagent = await genesis.spawn_subagent(
            parent=parent,
            role=role,
            task_objective=task_objective,
            timeout_seconds=timeout_seconds,
            token_budget=token_budget,
            model_provider=model_provider,
            model_name=model_name,
        )

        # Register in in-memory orchestrator if present
        if hasattr(ao, "_agents"):
            ao._agents[subagent.id] = subagent

        # Auto-create initial goal for child
        if task_objective:
            try:
                await ao.create_agent_goal(subagent.id, objective=task_objective)
            except Exception:
                pass

        logger.info(
            "agents_route.subagent_spawned",
            parent_id=parent.id,
            subagent_id=subagent.id,
            role=role,
        )

        return JSONResponse({
            "ok": True,
            "message": f"Ephemeral subagent '{subagent.name}' successfully spawned.",
            "subagent": _fmt_agent(subagent),
            "parent_id": parent.id,
            "active_subagents_count": len(parent.spawned_subagents),
        })

    except Exception as exc:
        logger.error("agents_route.spawn_subagent_error", error=str(exc)[:120])
        return JSONResponse({"ok": False, "error": str(exc).splitlines()[0][:120]}, status_code=500)


@router.post("/api/{agent_id}/reap_subagent")
async def agent_reap_subagent(request: Request, agent_id: str):
    """Reaps and archives an Ephemeral Subagent."""
    ao = _ao(request)
    if not ao:
        return JSONResponse({"ok": False, "error": "Agent orchestrator not available"}, status_code=503)
    try:
        parent = await ao.get_agent(agent_id)
        if not parent:
            return JSONResponse({"ok": False, "error": f"Parent agent '{agent_id}' not found"}, status_code=404)

        body = await request.json()
        subagent_id = (body.get("subagent_id") or "").strip()
        reason = (body.get("reason") or "operator_reap").strip()
        if not subagent_id:
            return JSONResponse({"ok": False, "error": "subagent_id is required"}, status_code=400)

        try:
            from ...agent_manager.genesis import SubagentGenesis
            from ...agent_manager.types import AgentStatus
        except (ImportError, ValueError):
            try:
                from src.agent_manager.genesis import SubagentGenesis
                from src.agent_manager.types import AgentStatus
            except (ImportError, ValueError):
                from agent_manager.genesis import SubagentGenesis
                from agent_manager.types import AgentStatus

        redis_url = getattr(request.app.state, "redis_url", "redis://localhost:6379/0")
        redis_client = getattr(request.app.state, "redis_client", None)
        genesis = SubagentGenesis(redis_url=redis_url, redis_client=redis_client)

        reaped = await genesis.reap_subagent(parent, subagent_id, reason=reason)

        if hasattr(ao, "_agents") and subagent_id in ao._agents:
            ao._agents[subagent_id].status = AgentStatus.ARCHIVED

        logger.info(
            "agents_route.subagent_reaped",
            parent_id=parent.id,
            subagent_id=subagent_id,
            reason=reason,
        )

        return JSONResponse({
            "ok": True,
            "message": f"Subagent '{subagent_id}' successfully reaped and archived.",
            "reaped": subagent_id,
            "parent_id": parent.id,
            "remaining_subagents_count": len(parent.spawned_subagents),
        })

    except Exception as exc:
        logger.error("agents_route.reap_subagent_error", error=str(exc)[:120])
        return JSONResponse({"ok": False, "error": str(exc).splitlines()[0][:120]}, status_code=500)


@router.post("/api/{agent_id}/delete")
async def agent_delete(request: Request, agent_id: str):
    """Hard-delete an agent and all its goals. Notifies the agent runtime."""
    ao = _ao(request)
    if not ao:
        return JSONResponse({"ok": False, "error": "Agent orchestrator not available"}, status_code=503)
    try:
        agent = await ao.get_agent(agent_id)
        if not agent:
            return JSONResponse({"ok": False, "error": "Agent not found"}, status_code=404)
        agent_name = agent.name

        # Cancel running goals first so the runtime doesn't keep ticking
        go = getattr(request.app.state, "goal_orchestrator", None)
        if go:
            try:
                import redis.asyncio as _ar
                from ...goal_orchestrator.store import list_goals as _list_goals
                _r = _ar.from_url(request.app.state.redis_url, decode_responses=True)
                try:
                    all_goals = await _list_goals(_r)
                    for g in all_goals:
                        if getattr(g, "agent_id", None) == agent_id:
                            await go.cancel_goal(g.id)
                finally:
                    await _r.aclose()
            except Exception:
                logger.exception("agents_route.delete_cancel_goals_error", agent_id=agent_id)

        ok = await ao.delete_agent(agent_id)
        if not ok:
            return JSONResponse({"ok": False, "error": "Delete failed"}, status_code=500)

        logger.info("agents_route.deleted", agent_id=agent_id, name=agent_name)
        return JSONResponse({"ok": True, "deleted": agent_name})
    except Exception as e:
        logger.exception("agents_route.delete_error")
        return JSONResponse({"ok": False, "error": str(e).splitlines()[0][:120]}, status_code=500)
