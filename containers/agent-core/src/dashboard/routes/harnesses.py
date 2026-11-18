"""Dashboard & botctl routes for Agent WASP's Harness-Agnostic CLI Engine.

Control-plane routes (dashboard session + CSRF protected):
  GET  /harnesses/api/status                   → Installed harnesses, active runs, pending handoffs
  GET  /harnesses/api/models/{harness}         → Available models & reasoning efforts
  GET  /harnesses/api/events                   → Unified SSE stream (tool activity, text deltas, handoffs)
  POST /harnesses/api/run/{agent_id}           → Dispatch a prompt to a harness-backed bot
  POST /harnesses/api/cancel/{agent_id}        → Abort an active harness run
  GET  /harnesses/api/handoffs                 → List handoff cards
  POST /harnesses/api/handoffs/{id}/resolve    → Mark a handoff Done (unblocks waiting bot)
  GET  /harnesses/api/memory/{agent_id}        → Read USER.md & bot MEMORY.md
  PUT  /harnesses/api/memory/{agent_id}        → Update bot MEMORY.md

Bot-internal `waspctl` routes (scoped per-run Bearer token via `WASP_TOKEN`):
  GET    /harnesses/botctl/bots                → List teammates
  POST   /harnesses/botctl/ask                 → Sync-with-timeout bot-to-bot ask
  POST   /harnesses/botctl/handoff             → Pause and wait for human operator handoff
  GET    /harnesses/botctl/routines            → List scheduled routines for this bot
  POST   /harnesses/botctl/routines            → Add a recurring routine (wired to custom_tasks)
  PATCH  /harnesses/botctl/routines/{id}       → Pause / resume routine
  DELETE /harnesses/botctl/routines/{id}       → Delete routine
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ...harnesses import ADAPTER_NAMES, HarnessRunner, ScreenClient, list_harness_models
from ...policy.approvals import ActionOrigin, ApprovalDecision, ApprovalManager, RuleKind, RuleScope
from ...scheduler import custom_tasks

router = APIRouter()
logger = structlog.get_logger()


def get_harness_runner(request: Request) -> HarnessRunner:
    runner = getattr(request.app.state, "harness_runner", None)
    if runner is None:
        runner = HarnessRunner()
        request.app.state.harness_runner = runner
    return runner


def get_approval_manager(request: Request) -> ApprovalManager:
    mgr = getattr(request.app.state, "approval_manager", None)
    if mgr is None:
        runner = get_harness_runner(request)
        mgr = ApprovalManager(
            rules_path=runner.data_dir / "approval_rules.json",
            broadcast=runner.broadcast,
        )
        request.app.state.approval_manager = mgr
    return mgr


def get_screen_client(request: Request) -> ScreenClient:
    client = getattr(request.app.state, "screen_client", None)
    if client is None:
        client = ScreenClient()
        request.app.state.screen_client = client
    return client


def _require_botctl_auth(request: Request) -> tuple[str | None, JSONResponse | None]:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None, JSONResponse({"ok": False, "error": "Missing bearer token"}, status_code=401)
    token = auth_header[7:].strip()
    runner = get_harness_runner(request)
    bot_id = runner.verify_token(token)
    if not bot_id:
        return None, JSONResponse({"ok": False, "error": "Invalid or expired botctl token"}, status_code=401)
    return bot_id, None


async def _get_redis_or_none(request: Request):
    redis_url = getattr(request.app.state, "redis_url", "") or ""
    if not redis_url:
        return None
    try:
        r = aioredis.from_url(redis_url, decode_responses=True)
        return r
    except Exception:
        return None


def _memory_routines(request: Request) -> dict[str, dict[str, Any]]:
    store = getattr(request.app.state, "_botctl_routines_fallback", None)
    if store is None:
        store = {}
        request.app.state._botctl_routines_fallback = store
    return store


async def _resolve_agent(ao, identifier: str):
    if ao is None:
        return None
    target = (identifier or "").strip()
    if not target:
        return None
    agent = await ao.get_agent(target)
    if agent is not None:
        return agent
    agents = await ao.list_agents()
    lower = target.lower()
    for a in agents:
        if a.name.lower() == lower:
            return a
    return None


async def _persist_session_id(request: Request, agent, session_id: str | None) -> None:
    if not session_id or not agent:
        return
    if getattr(agent, "harness_session_id", "") == session_id:
        return
    agent.harness_session_id = session_id
    redis_url = getattr(request.app.state, "redis_url", "") or ""
    if not redis_url:
        return
    try:
        from ...agent_manager.store import save_agent
        r = aioredis.from_url(redis_url, decode_responses=True)
        try:
            await save_agent(r, agent)
        finally:
            await r.aclose()
    except Exception as exc:
        logger.debug("harnesses.session_persist_skipped", agent_id=agent.id, error=str(exc)[:80])


# ---------------------------------------------------------------------------
# Control-plane / Dashboard API
# ---------------------------------------------------------------------------


@router.get("/api/status")
async def harness_status(request: Request):
    runner = get_harness_runner(request)
    approvals = get_approval_manager(request)
    return JSONResponse(
        {
            "ok": True,
            **runner.status_snapshot(),
            "pending_approvals": approvals.list_cards(pending_only=True),
            "auto_review_enabled": approvals.auto_review_enabled,
        }
    )


@router.get("/api/models/{harness_name}")
async def harness_models(request: Request, harness_name: str, refresh: bool = False):
    key = harness_name.strip().lower()
    if key not in ADAPTER_NAMES:
        return JSONResponse(
            {"ok": False, "error": f"Unknown harness '{harness_name}'"},
            status_code=404,
        )
    models = await asyncio.to_thread(list_harness_models, key, refresh)
    return JSONResponse({"ok": True, "harness": key, "models": [m.to_dict() for m in models]})


@router.get("/api/events")
async def harness_events(request: Request):
    """Unified Server-Sent Events stream for live harness activity and handoffs."""
    runner = get_harness_runner(request)
    queue = runner.subscribe()

    async def _stream():
        try:
            yield f"data: {json.dumps({'type': 'snapshot', **runner.status_snapshot()})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=20.0)
                    yield f"data: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            runner.unsubscribe(queue)

    return StreamingResponse(_stream(), media_type="text/event-stream")


@router.post("/api/run/{agent_id}")
async def harness_run_agent(request: Request, agent_id: str):
    """Run a prompt on an agent using its configured (or overridden) CLI harness."""
    runner = get_harness_runner(request)
    ao = getattr(request.app.state, "agent_orchestrator", None)
    body = await request.json()
    prompt = (body.get("prompt") or body.get("message") or "").strip()
    if not prompt:
        return JSONResponse({"ok": False, "error": "prompt is required"}, status_code=400)

    agent = await _resolve_agent(ao, agent_id)
    harness_name = (body.get("harness") or (getattr(agent, "harness", "") if agent else "") or "").strip().lower()
    if not harness_name:
        return JSONResponse(
            {"ok": False, "error": "No harness specified on request or agent"},
            status_code=400,
        )

    bot_id = agent.id if agent else agent_id
    bot_name = agent.name if agent else (body.get("bot_name") or agent_id)
    # New user message in this chat closes any stale pending approval cards for this bot.
    get_approval_manager(request).close_open_cards_for_bot(bot_id)
    model = body.get("model") or (getattr(agent, "model_name", "") if agent else "") or None
    effort = body.get("effort") or (getattr(agent, "reasoning_effort", "") if agent else "") or None
    session_id = body.get("session_id") or (getattr(agent, "harness_session_id", "") if agent else "") or None
    timeout = float(body.get("timeout", runner.default_timeout))
    wait = bool(body.get("wait", True))

    async def _exec():
        result = await runner.run_bot(
            bot_id=bot_id,
            bot_name=bot_name,
            harness_name=harness_name,
            prompt=prompt,
            identity_prompt=getattr(agent, "identity_prompt", "") if agent else "",
            description=getattr(agent, "description", "") if agent else "",
            session_id=session_id,
            model=model,
            effort=effort,
            timeout_seconds=timeout,
        )
        await _persist_session_id(request, agent, result.session_id)
        return result

    if not wait:
        asyncio.create_task(_exec())
        return JSONResponse({"ok": True, "queued": True, "bot_id": bot_id, "harness": harness_name})

    result = await _exec()
    return JSONResponse(
        {
            "ok": result.ok,
            "bot_id": bot_id,
            "harness": harness_name,
            "text": result.text,
            "session_id": result.session_id,
            "error": result.error,
            "aborted_reason": result.aborted_reason,
        },
        status_code=200 if result.ok else 502,
    )


@router.post("/api/cancel/{agent_id}")
async def harness_cancel(request: Request, agent_id: str):
    runner = get_harness_runner(request)
    cancelled = runner.cancel_run(agent_id)
    return JSONResponse({"ok": cancelled, "agent_id": agent_id})


@router.get("/api/handoffs")
async def list_handoffs(request: Request, pending_only: bool = False):
    runner = get_harness_runner(request)
    return JSONResponse({"ok": True, "handoffs": runner.list_handoffs(pending_only=pending_only)})


@router.post("/api/handoffs/{handoff_id}/resolve")
async def resolve_handoff(request: Request, handoff_id: str):
    runner = get_harness_runner(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    note = str(body.get("note") or "Done")
    resolved = runner.resolve_handoff(handoff_id, note=note)
    if not resolved:
        return JSONResponse({"ok": False, "error": "Pending handoff not found"}, status_code=404)
    return JSONResponse({"ok": True, "handoff": resolved.to_dict()})


@router.get("/api/memory/{agent_id}")
async def get_bot_memory(request: Request, agent_id: str):
    runner = get_harness_runner(request)
    ao = getattr(request.app.state, "agent_orchestrator", None)
    agent = await _resolve_agent(ao, agent_id)
    mem = runner.read_memory(agent.id if agent else agent_id, agent.name if agent else agent_id)
    return JSONResponse({"ok": True, **mem})


@router.put("/api/memory/{agent_id}")
async def update_bot_memory(request: Request, agent_id: str):
    runner = get_harness_runner(request)
    body = await request.json()
    content = body.get("memory_md")
    if not isinstance(content, str):
        return JSONResponse({"ok": False, "error": "memory_md string is required"}, status_code=400)
    path = runner.write_bot_memory(agent_id, content)
    return JSONResponse({"ok": True, "bot_path": str(path)})


@router.get("/api/approvals")
async def list_approvals(request: Request, pending_only: bool = False):
    mgr = get_approval_manager(request)
    return JSONResponse(
        {
            "ok": True,
            "auto_review_enabled": mgr.auto_review_enabled,
            "cards": mgr.list_cards(pending_only=pending_only),
        }
    )


@router.post("/api/approvals/{card_id}/decide")
async def decide_approval(request: Request, card_id: str):
    mgr = get_approval_manager(request)
    body = await request.json()
    decision_raw = str(body.get("decision") or "").strip().lower()
    note = str(body.get("note") or "")
    actor_id = str(getattr(request.state, "user_id", "") or body.get("actor_id") or "operator")
    try:
        dec = ApprovalDecision(decision_raw)
    except ValueError:
        return JSONResponse(
            {"ok": False, "error": "decision must be one of: allow_once, always_allow, deny"},
            status_code=400,
        )
    try:
        card = mgr.decide_card(card_id, dec, actor_id=actor_id, note=note)
    except KeyError:
        return JSONResponse({"ok": False, "error": "Pending approval card not found"}, status_code=404)
    except PermissionError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=403)
    return JSONResponse({"ok": True, "card": card.to_dict()})


@router.get("/api/approval-rules")
async def list_approval_rules(request: Request):
    mgr = get_approval_manager(request)
    return JSONResponse({"ok": True, "rules": mgr.list_rules()})


@router.post("/api/approval-rules")
async def create_approval_rule(request: Request):
    mgr = get_approval_manager(request)
    body = await request.json()
    try:
        rule = mgr.add_rule(
            kind=RuleKind(str(body.get("kind") or "ask_first")),
            scope=RuleScope(str(body.get("scope") or "personal")),
            text=str(body.get("text") or ""),
            action_pattern=str(body.get("action_pattern") or ""),
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "rule": rule.to_dict()})


@router.delete("/api/approval-rules/{rule_id}")
async def delete_approval_rule(request: Request, rule_id: str):
    mgr = get_approval_manager(request)
    if not mgr.delete_rule(rule_id):
        return JSONResponse({"ok": False, "error": "Approval rule not found"}, status_code=404)
    return JSONResponse({"ok": True, "deleted": rule_id})


@router.get("/api/screens")
async def list_screens(request: Request):
    client = get_screen_client(request)
    return JSONResponse(await client.list_screens())


@router.post("/api/screens/{bot_id}/open")
async def open_bot_screen(request: Request, bot_id: str):
    client = get_screen_client(request)
    res = await client.ensure_screen(bot_id)
    code = 200 if res.get("ok") else (503 if not res.get("available") else 502)
    return JSONResponse(res, status_code=code)


@router.delete("/api/screens/{bot_id}")
async def close_bot_screen(request: Request, bot_id: str):
    client = get_screen_client(request)
    res = await client.close_screen(bot_id)
    return JSONResponse(res, status_code=200 if res.get("ok") else 404)


# ---------------------------------------------------------------------------
# Bot-internal `waspctl` API (authenticated via scoped per-run Bearer token)
# ---------------------------------------------------------------------------


@router.get("/botctl/bots")
async def botctl_list_bots(request: Request):
    caller_id, err = _require_botctl_auth(request)
    if err:
        return err
    ao = getattr(request.app.state, "agent_orchestrator", None)
    runner = get_harness_runner(request)
    bots: list[dict[str, Any]] = []
    if ao is not None:
        for agent in await ao.list_agents():
            bots.append(
                {
                    "id": agent.id,
                    "name": agent.name,
                    "description": agent.description,
                    "harness": getattr(agent, "harness", ""),
                    "model": agent.model_name,
                    "status": agent.status.value if hasattr(agent.status, "value") else str(agent.status),
                    "working": runner.is_running(agent.id),
                }
            )
    return JSONResponse({"ok": True, "caller": caller_id, "bots": bots})


@router.post("/botctl/ask")
async def botctl_ask(request: Request):
    """Synchronous-with-timeout Bot-to-Bot request-reply backed by WASP's bus & runner."""
    caller_id, err = _require_botctl_auth(request)
    if err:
        return err
    body = await request.json()
    target_ref = str(body.get("to") or "").strip()
    text = str(body.get("text") or "").strip()
    timeout = min(max(float(body.get("timeout", 120.0)), 1.0), 600.0)
    if not target_ref or not text:
        return JSONResponse({"ok": False, "error": "Both 'to' and 'text' are required"}, status_code=400)

    ao = getattr(request.app.state, "agent_orchestrator", None)
    runner = get_harness_runner(request)
    target = await _resolve_agent(ao, target_ref)
    if target is None:
        return JSONResponse({"ok": False, "error": f"Bot '{target_ref}' not found"}, status_code=404)
    if target.id == caller_id:
        return JSONResponse({"ok": False, "error": "A bot cannot ask itself"}, status_code=400)

    # Best-effort persistence on WASP's durable PostgreSQL agent bus
    try:
        from ...agent_manager.bus import send_message as bus_send_message
        await bus_send_message(
            from_agent_id=caller_id,
            to_agent_id=target.id,
            content=text,
            message_type="request",
        )
    except Exception as exc:
        logger.debug("botctl.ask_bus_persist_skipped", error=str(exc)[:80])

    # If the target uses a CLI harness, run synchronously under its FIFO lock
    target_harness = (getattr(target, "harness", "") or "").strip().lower()
    if target_harness:
        prompt = f"[Message from teammate bot `{caller_id}`]:\n{text}"
        result = await runner.run_bot(
            bot_id=target.id,
            bot_name=target.name,
            harness_name=target_harness,
            prompt=prompt,
            identity_prompt=target.identity_prompt,
            description=target.description,
            session_id=getattr(target, "harness_session_id", "") or None,
            model=target.model_name or None,
            effort=getattr(target, "reasoning_effort", "") or None,
            timeout_seconds=timeout,
        )
        await _persist_session_id(request, target, result.session_id)
        if result.ok:
            return JSONResponse({"ok": True, "mode": "sync", "to": target.id, "reply": result.text})
        return JSONResponse(
            {"ok": False, "mode": "sync", "to": target.id, "error": result.error or "Target bot failed"},
            status_code=502,
        )

    # Otherwise delegate asynchronously to WASP's native GoalOrchestrator
    if ao is not None:
        goal = await ao.create_agent_goal(target.id, objective=f"[Request from {caller_id}] {text}")
        return JSONResponse(
            {
                "ok": True,
                "mode": "async_goal",
                "to": target.id,
                "goal_id": goal.id,
                "reply": f"Delegated asynchronously to {target.name} (goal_id={goal.id}).",
            }
        )
    return JSONResponse({"ok": False, "error": "Agent orchestrator unavailable"}, status_code=503)


@router.post("/botctl/approve")
async def botctl_approve(request: Request):
    """Evaluate an action through Auto Review and, if risky, await an Approval Card."""
    caller_id, err = _require_botctl_auth(request)
    if err:
        return err
    body = await request.json()
    action_name = str(body.get("action_name") or "").strip()
    action_summary = str(body.get("action_summary") or "").strip()
    action_detail = str(body.get("action_detail") or "").strip()
    origin_raw = str(body.get("origin") or "user_direct").strip().lower()
    requester_id = str(body.get("requester_id") or "").strip()
    raw_timeout = body.get("timeout")
    timeout = min(max(float(raw_timeout), 1.0), 3600.0) if raw_timeout is not None else None

    if not action_name or not action_summary:
        return JSONResponse(
            {"ok": False, "error": "Both 'action_name' and 'action_summary' are required"},
            status_code=400,
        )
    try:
        origin = ActionOrigin(origin_raw)
    except ValueError:
        return JSONResponse(
            {"ok": False, "error": f"Invalid origin '{origin_raw}'"},
            status_code=400,
        )

    ao = getattr(request.app.state, "agent_orchestrator", None)
    agent = await _resolve_agent(ao, caller_id)
    bot_name = agent.name if agent else caller_id

    mgr = get_approval_manager(request)

    # Best-effort notify outgoing bus (e.g. Telegram) when a card is likely needed
    bus = getattr(request.app.state, "bus", None)
    preview = await mgr.review_action(action_name, action_summary, action_detail, origin=origin)
    if preview.requires_card and bus is not None and hasattr(bus, "publish"):
        try:
            await bus.publish(
                "events:outgoing",
                {
                    "event_type": "BOT_APPROVAL_REQUESTED",
                    "agent_id": caller_id,
                    "agent_name": bot_name,
                    "action_name": action_name,
                    "detail": action_summary,
                    "risk_category": preview.risk_category.value,
                    "warning": preview.warning,
                },
            )
        except Exception:
            pass

    approved, review, card = await mgr.request_approval(
        bot_id=caller_id,
        bot_name=bot_name,
        action_name=action_name,
        action_summary=action_summary,
        action_detail=action_detail,
        origin=origin,
        requester_id=requester_id,
        timeout_seconds=timeout,
    )
    return JSONResponse(
        {
            "ok": True,
            "approved": approved,
            "auto": card is None,
            "reason": review.reason,
            "review": review.to_dict(),
            "card": card.to_dict() if card else None,
        },
        status_code=200 if approved else 403,
    )


@router.post("/botctl/screen")
async def botctl_screen(request: Request):
    """Ensure and return this bot's isolated virtual screen info & live takeover URL."""
    caller_id, err = _require_botctl_auth(request)
    if err:
        return err
    client = get_screen_client(request)
    res = await client.ensure_screen(caller_id, busy=True)
    code = 200 if res.get("ok") else 503
    return JSONResponse(res, status_code=code)


@router.post("/botctl/handoff")
async def botctl_handoff(request: Request):
    """Pause the calling bot and wait for the human operator to resolve the handoff card."""
    caller_id, err = _require_botctl_auth(request)
    if err:
        return err
    body = await request.json()
    reason = str(body.get("text") or "").strip()
    timeout = min(max(float(body.get("timeout", 600.0)), 1.0), 1800.0)
    if not reason:
        return JSONResponse({"ok": False, "error": "'text' is required"}, status_code=400)

    ao = getattr(request.app.state, "agent_orchestrator", None)
    agent = await _resolve_agent(ao, caller_id)
    bot_name = agent.name if agent else caller_id

    runner = get_harness_runner(request)

    # Best-effort attach live screen URL if isolated `agent-screens` container is running
    screen_url = ""
    try:
        sc = get_screen_client(request)
        sc_res = await sc.ensure_screen(caller_id, busy=True)
        if sc_res.get("ok") and isinstance(sc_res.get("screen"), dict):
            screen_url = str(sc_res["screen"].get("view_url") or "")
    except Exception:
        screen_url = ""

    # Best-effort notify outgoing event bus (e.g., Telegram bridge)
    bus = getattr(request.app.state, "bus", None)
    if bus is not None and hasattr(bus, "publish"):
        try:
            await bus.publish(
                "events:outgoing",
                {
                    "event_type": "BOT_HANDOFF_REQUESTED",
                    "agent_id": caller_id,
                    "agent_name": bot_name,
                    "detail": reason,
                    "screen_url": screen_url,
                },
            )
        except Exception:
            pass

    handoff, resolved = await runner.request_handoff(
        bot_id=caller_id,
        bot_name=bot_name,
        reason=reason,
        screen_url=screen_url,
        timeout_seconds=timeout,
    )
    if not resolved:
        return JSONResponse(
            {"ok": False, "status": "expired", "handoff": handoff.to_dict(), "error": "Handoff timed out"},
            status_code=408,
        )
    return JSONResponse(
        {
            "ok": True,
            "status": "resolved",
            "note": handoff.resolution_note,
            "handoff": handoff.to_dict(),
        }
    )


@router.get("/botctl/routines")
async def botctl_list_routines(request: Request):
    caller_id, err = _require_botctl_auth(request)
    if err:
        return err
    r = await _get_redis_or_none(request)
    if r is not None:
        try:
            tasks = await custom_tasks.list_tasks(r)
        finally:
            await r.aclose()
    else:
        tasks = list(_memory_routines(request).values())

    mine = [t for t in tasks if not t.get("agent_id") or t.get("agent_id") == caller_id]
    return JSONResponse({"ok": True, "routines": mine})


@router.post("/botctl/routines")
async def botctl_add_routine(request: Request):
    caller_id, err = _require_botctl_auth(request)
    if err:
        return err
    body = await request.json()
    name = str(body.get("name") or "").strip()
    schedule = str(body.get("schedule") or body.get("cron") or "").strip()
    instruction = str(body.get("instruction") or "").strip()
    if not name or not schedule or not instruction:
        return JSONResponse(
            {"ok": False, "error": "name, schedule, and instruction are required"},
            status_code=400,
        )

    interval = custom_tasks.parse_interval(schedule)
    if interval is None or interval < 60:
        return JSONResponse(
            {"ok": False, "error": f"Could not parse schedule '{schedule}' (use e.g. 'every 1h', 'every 15m', 'daily')"},
            status_code=400,
        )

    task = custom_tasks.make_task(
        name=name,
        instruction=instruction,
        interval_seconds=interval,
        agent_id=caller_id,
    )
    r = await _get_redis_or_none(request)
    if r is not None:
        try:
            await custom_tasks.save_task(r, task)
        finally:
            await r.aclose()
    else:
        _memory_routines(request)[task["task_id"]] = task

    return JSONResponse({"ok": True, "id": task["task_id"], "routine": task})


@router.patch("/botctl/routines/{routine_id}")
async def botctl_patch_routine(request: Request, routine_id: str):
    caller_id, err = _require_botctl_auth(request)
    if err:
        return err
    body = await request.json()
    enabled = bool(body.get("enabled", True))
    r = await _get_redis_or_none(request)
    if r is not None:
        try:
            task = await custom_tasks.get_task(r, routine_id)
            if not task or (task.get("agent_id") and task.get("agent_id") != caller_id):
                return JSONResponse({"ok": False, "error": "Routine not found"}, status_code=404)
            task["enabled"] = enabled
            await custom_tasks.save_task(r, task)
        finally:
            await r.aclose()
    else:
        store = _memory_routines(request)
        task = store.get(routine_id)
        if not task or (task.get("agent_id") and task.get("agent_id") != caller_id):
            return JSONResponse({"ok": False, "error": "Routine not found"}, status_code=404)
        task["enabled"] = enabled
    return JSONResponse({"ok": True, "routine": task})


@router.delete("/botctl/routines/{routine_id}")
async def botctl_delete_routine(request: Request, routine_id: str):
    caller_id, err = _require_botctl_auth(request)
    if err:
        return err
    r = await _get_redis_or_none(request)
    if r is not None:
        try:
            task = await custom_tasks.get_task(r, routine_id)
            if not task or (task.get("agent_id") and task.get("agent_id") != caller_id):
                return JSONResponse({"ok": False, "error": "Routine not found"}, status_code=404)
            await custom_tasks.delete_task(r, routine_id)
        finally:
            await r.aclose()
    else:
        store = _memory_routines(request)
        task = store.get(routine_id)
        if not task or (task.get("agent_id") and task.get("agent_id") != caller_id):
            return JSONResponse({"ok": False, "error": "Routine not found"}, status_code=404)
        store.pop(routine_id, None)
    return JSONResponse({"ok": True, "deleted": routine_id})
