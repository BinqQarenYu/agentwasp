"""Autonomous Goal Engine — dashboard routes.

Advanced control layer extensions:
  - _fmt_goal: includes autonomy_mode, budget, stability, telemetry
  - POST /set_autonomy  — change autonomy mode on a live goal
  - POST /save_template — save a completed goal as a reusable template
  - GET  /api/templates — list all saved templates
  - GET  /api/telemetry/{goal_id} — per-goal telemetry JSON
"""

from __future__ import annotations

import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ...goal_orchestrator.store import list_goals, load_goal, delete_goal as hard_delete_goal

router = APIRouter()
logger = structlog.get_logger()


def _orchestrator(request: Request):
    return getattr(request.app.state, "goal_orchestrator", None)


def _fmt_goal(goal) -> dict:
    """Convert a Goal object to a template-friendly dict (includes advanced layer fields)."""
    failed_tasks = goal.task_graph.get_failed_tasks() if goal.task_graph else []
    pending = goal.task_graph.get_pending_tasks() if goal.task_graph else []
    current_task = goal.task_graph.get_next_task() if goal.task_graph else None

    # Budget percentages (safe division)
    budget = goal.budget
    budget_pct_planning = 0.0
    if budget.max_tokens_planning > 0:
        budget_pct_planning = min(100.0, budget.tokens_used_planning / budget.max_tokens_planning * 100)
    budget_pct_steps = 0.0
    if budget.max_steps > 0:
        budget_pct_steps = min(100.0, budget.steps_executed / budget.max_steps * 100)

    # Stability
    stab = goal.stability

    # Telemetry
    tel = goal.telemetry

    return {
        "id": goal.id,
        "title": goal.title or goal.objective[:50],
        "objective": goal.objective,
        "constraints": goal.constraints,
        "success_criteria": goal.success_criteria,
        "state": goal.state.value,
        "progress": goal.progress,
        "step_count": goal.step_count,
        "replan_count": goal.replan_count,
        "max_steps": goal.max_steps,
        "created_at": goal.created_at[:19] if goal.created_at else "",
        "updated_at": goal.updated_at[:19] if goal.updated_at else "",
        "started_at": goal.started_at[:19] if goal.started_at else "",
        "completed_at": goal.completed_at[:19] if goal.completed_at else "",
        "error": goal.error,
        "chat_id": goal.chat_id,
        "total_tasks": goal.task_graph.total_tasks if goal.task_graph else 0,
        "completed_tasks": sum(1 for n in (goal.task_graph.nodes if goal.task_graph else []) if n.status.value == "done"),
        "failed_tasks": len(failed_tasks),
        "pending_tasks": len(pending),
        "current_task_desc": current_task.description[:80] if current_task else "",
        "current_skill": current_task.skill_name if current_task else "",
        "runtime_s": int(goal.runtime_seconds()),
        # --- Advanced control layer fields ---
        "autonomy_mode": goal.autonomy_mode.value,
        "budget_pct_planning": round(budget_pct_planning, 1),
        "budget_pct_steps": round(budget_pct_steps, 1),
        "budget_tokens_used_planning": budget.tokens_used_planning,
        "budget_max_tokens_planning": budget.max_tokens_planning,
        "budget_steps_executed": budget.steps_executed,
        "budget_exceeded": budget.budget_exceeded,
        "budget_exceeded_dimension": budget.budget_exceeded_dimension,
        "stability_consecutive_failures": stab.consecutive_failures,
        "stability_locked": stab.locked,
        "stability_in_backoff": stab.in_backoff,
        "stability_intervention_reason": stab.intervention_reason,
        "telemetry_policy_blocks": tel.policy_blocks,
        "telemetry_budget_exceeded_events": tel.budget_exceeded_events,
        "telemetry_stability_interventions": tel.stability_interventions,
        "telemetry_autonomy_decisions": tel.autonomy_decisions,
        "telemetry_skill_distribution": tel.skill_distribution,
        "template_id": tel.template_id,
        "template_name": tel.template_name,
        # Priority arbitration
        "priority": getattr(goal, "priority", 5),
        "source": getattr(goal, "source", "user"),
        # Node list (task DAG)
        "nodes": [
            {
                "id": n.id,
                "description": n.description[:60],
                "skill_name": n.skill_name,
                "status": n.status.value,
                "risk_level": n.risk_level.value,
                "retries": n.retries,
                "max_retries": n.max_retries,
                "output_summary": (n.output_summary or "")[:100],
                "error": (n.error or "")[:80],
                "dependencies": n.dependencies,
            }
            for n in (goal.task_graph.nodes if goal.task_graph else [])
        ],
    }


_USER_SOURCES = ("user", "dashboard", "goal")  # sources shown in the Goals section


def _is_user_goal(goal) -> bool:
    return getattr(goal, "source", "user") not in ("agent", "autonomous")


def _compute_stats(goals_data: list[dict]) -> dict:
    total = len(goals_data)
    active = sum(1 for g in goals_data if g["state"] in ("active", "planning"))
    completed = sum(1 for g in goals_data if g["state"] == "completed")
    failed = sum(1 for g in goals_data if g["state"] in ("failed", "cancelled"))
    return {"total": total, "active": active, "completed": completed, "failed": failed}


def _get_redis(request: Request):
    return aioredis.from_url(request.app.state.redis_url, decode_responses=True)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def goals_page(request: Request):
    r = _get_redis(request)
    try:
        goals = await list_goals(r)
        # Also load templates for the create modal
        from ...goal_orchestrator.templates import list_templates
        templates = await list_templates(r)
    finally:
        await r.aclose()

    goals = [g for g in goals if _is_user_goal(g)]
    goals_data = [_fmt_goal(g) for g in goals]
    templates_data = [
        {
            "id": t.id,
            "name": t.name,
            "description": t.description,
            "task_count": t.task_count,
            "success_rate": round(t.success_rate * 100, 0),
            "use_count": t.use_count,
        }
        for t in templates
    ]
    return request.app.state.templates.TemplateResponse(request, "goals.html", {
        "goals": goals_data,
        "templates": templates_data,
        "goal_engine_enabled": _orchestrator(request) is not None,
        "stats": _compute_stats(goals_data),
    })


# ---------------------------------------------------------------------------
# API — Goal list + detail
# ---------------------------------------------------------------------------


@router.get("/api/status")
async def goals_api_status(request: Request):
    r = _get_redis(request)
    try:
        goals = await list_goals(r)
    finally:
        await r.aclose()
    goals = [g for g in goals if _is_user_goal(g)]
    goals_data = [_fmt_goal(g) for g in goals]
    return JSONResponse({**_compute_stats(goals_data), "goals": goals_data})


@router.get("/api/list")
async def goals_api_list(request: Request):
    r = _get_redis(request)
    try:
        goals = await list_goals(r)
    finally:
        await r.aclose()
    goals = [g for g in goals if _is_user_goal(g)]
    return JSONResponse({"ok": True, "goals": [_fmt_goal(g) for g in goals]})


@router.get("/api/detail/{goal_id}")
async def goal_detail_api(request: Request, goal_id: str):
    r = _get_redis(request)
    try:
        goal = await load_goal(r, goal_id)
    finally:
        await r.aclose()

    if goal is None:
        return JSONResponse({"ok": False, "error": "Goal not found"}, status_code=404)
    return JSONResponse({"ok": True, "goal": _fmt_goal(goal)})


@router.get("/api/telemetry/{goal_id}")
async def goal_telemetry_api(request: Request, goal_id: str):
    """Return full telemetry JSON for a goal."""
    r = _get_redis(request)
    try:
        goal = await load_goal(r, goal_id)
    finally:
        await r.aclose()

    if goal is None:
        return JSONResponse({"ok": False, "error": "Goal not found"}, status_code=404)

    return JSONResponse({
        "ok": True,
        "goal_id": goal_id,
        "autonomy_mode": goal.autonomy_mode.value,
        "budget": goal.budget.model_dump(),
        "stability": goal.stability.model_dump(),
        "telemetry": goal.telemetry.model_dump(),
    })


# ---------------------------------------------------------------------------
# API — Templates
# ---------------------------------------------------------------------------


@router.get("/api/templates")
async def templates_api_list(request: Request):
    r = _get_redis(request)
    try:
        from ...goal_orchestrator.templates import list_templates
        templates = await list_templates(r)
    finally:
        await r.aclose()

    return JSONResponse({
        "ok": True,
        "templates": [
            {
                "id": t.id,
                "name": t.name,
                "description": t.description,
                "task_count": t.task_count,
                "success_rate": round(t.success_rate * 100, 1),
                "use_count": t.use_count,
                "created_at": t.created_at[:19] if t.created_at else "",
                "last_used": t.last_used[:19] if t.last_used else "",
            }
            for t in templates
        ],
    })


@router.post("/save_template")
async def save_template(request: Request):
    """Save a completed goal as a reusable template."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    goal_id = (body.get("goal_id") or "").strip()
    name = (body.get("name") or "").strip()
    if not goal_id:
        return JSONResponse({"ok": False, "error": "goal_id is required"})
    if not name:
        return JSONResponse({"ok": False, "error": "name is required"})

    r = _get_redis(request)
    try:
        goal = await load_goal(r, goal_id)
        if goal is None:
            return JSONResponse({"ok": False, "error": "Goal not found"}, status_code=404)

        from ...goal_orchestrator.templates import make_template_from_goal, save_template as _save_tpl
        template = make_template_from_goal(goal, name=name)
        await _save_tpl(r, template)
    finally:
        await r.aclose()

    logger.info("goals.template_saved", goal_id=goal_id, template_id=template.id, name=name)
    return JSONResponse({"ok": True, "template_id": template.id, "name": template.name})


@router.post("/delete_template")
async def delete_template(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    template_id = (body.get("template_id") or "").strip()
    if not template_id:
        return JSONResponse({"ok": False, "error": "template_id is required"})

    r = _get_redis(request)
    try:
        from ...goal_orchestrator.templates import delete_template as _del_tpl
        await _del_tpl(r, template_id)
    finally:
        await r.aclose()

    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# API — Goal lifecycle
# ---------------------------------------------------------------------------


@router.post("/create")
async def create_goal(request: Request):
    orch = _orchestrator(request)
    if orch is None:
        return JSONResponse({"ok": False, "error": "Goal engine not available"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    objective = (body.get("objective") or "").strip()
    if not objective:
        return JSONResponse({"ok": False, "error": "objective is required"})

    from ...config import settings
    from ...goal_orchestrator.types import AutonomyMode
    chat_id = (body.get("chat_id") or settings.scheduler_notify_chat_id).strip()

    # Parse autonomy_mode
    autonomy_mode = None
    raw_mode = (body.get("autonomy_mode") or "").strip().lower()
    if raw_mode in ("assist", "semi", "full"):
        autonomy_mode = AutonomyMode(raw_mode)

    template_id = (body.get("template_id") or "").strip() or None

    try:
        goal = await orch.create_goal(
            objective=objective,
            chat_id=chat_id,
            user_id=(body.get("user_id") or "dashboard").strip(),
            constraints=(body.get("constraints") or "").strip(),
            success_criteria=(body.get("success_criteria") or "").strip(),
            max_steps=int(body.get("max_steps") or settings.goal_default_max_steps),
            max_runtime_seconds=int(body.get("max_runtime_seconds") or settings.goal_default_max_runtime),
            autonomy_mode=autonomy_mode,
            template_id=template_id,
            priority=8,
            source="dashboard",
        )
    except Exception as e:
        logger.exception("goals.create_error")
        safe_msg = str(e)[:120].split("\n")[0]  # first line only, no stack traces
        return JSONResponse({"ok": False, "error": safe_msg}, status_code=500)

    logger.info("goals.created", goal_id=goal.id, objective=objective[:80])
    return JSONResponse({
        "ok": True,
        "goal_id": goal.id,
        "state": goal.state.value,
        "tasks": goal.task_graph.total_tasks if goal.task_graph else 0,
        "autonomy_mode": goal.autonomy_mode.value,
    })


@router.post("/pause")
async def pause_goal(request: Request):
    orch = _orchestrator(request)
    if orch is None:
        return JSONResponse({"ok": False, "error": "Goal engine not available"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    goal_id = (body.get("goal_id") or "").strip()
    if not goal_id:
        return JSONResponse({"ok": False, "error": "goal_id is required"})

    ok, msg = await orch.pause_goal(goal_id)
    return JSONResponse({"ok": ok, "message": msg})


@router.post("/resume")
async def resume_goal(request: Request):
    orch = _orchestrator(request)
    if orch is None:
        return JSONResponse({"ok": False, "error": "Goal engine not available"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    goal_id = (body.get("goal_id") or "").strip()
    if not goal_id:
        return JSONResponse({"ok": False, "error": "goal_id is required"})

    ok, msg = await orch.resume_goal(goal_id)
    return JSONResponse({"ok": ok, "message": msg})


@router.post("/cancel")
async def cancel_goal(request: Request):
    orch = _orchestrator(request)
    if orch is None:
        return JSONResponse({"ok": False, "error": "Goal engine not available"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    goal_id = (body.get("goal_id") or "").strip()
    if not goal_id:
        return JSONResponse({"ok": False, "error": "goal_id is required"})

    ok, msg = await orch.cancel_goal(goal_id)
    return JSONResponse({"ok": ok, "message": msg})


@router.post("/delete")
async def delete_goal(request: Request):
    """Hard-delete a goal from Redis regardless of state."""
    orch = _orchestrator(request)
    if orch is None:
        return JSONResponse({"ok": False, "error": "Goal engine not available"}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    goal_id = (body.get("goal_id") or "").strip()
    if not goal_id:
        return JSONResponse({"ok": False, "error": "goal_id is required"})

    # Cancel first if still active so the runtime stops ticking it
    try:
        await orch.cancel_goal(goal_id)
    except Exception:
        pass  # already completed/failed — fine

    # Hard-delete from Redis
    r = await orch._redis()
    try:
        deleted = await hard_delete_goal(r, goal_id)
    finally:
        await r.aclose()

    if not deleted:
        return JSONResponse({"ok": False, "error": "Goal not found"}, status_code=404)

    logger.info("goals_route.deleted", goal_id=goal_id)
    return JSONResponse({"ok": True, "deleted": goal_id})


@router.post("/set_autonomy")
async def set_autonomy(request: Request):
    """Switch autonomy mode on an active or paused goal."""
    orch = _orchestrator(request)
    if orch is None:
        return JSONResponse({"ok": False, "error": "Goal engine not available"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    goal_id = (body.get("goal_id") or "").strip()
    raw_mode = (body.get("autonomy_mode") or "").strip().lower()
    if not goal_id:
        return JSONResponse({"ok": False, "error": "goal_id is required"})
    if raw_mode not in ("assist", "semi", "full"):
        return JSONResponse({"ok": False, "error": "autonomy_mode must be assist|semi|full"})

    from ...goal_orchestrator.types import AutonomyMode
    ok, msg = await orch.set_autonomy_mode(goal_id, AutonomyMode(raw_mode))
    return JSONResponse({"ok": ok, "message": msg})
