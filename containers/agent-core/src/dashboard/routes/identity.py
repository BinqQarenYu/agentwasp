"""Dashboard routes — Agent Identity Engine."""

from uuid import uuid4

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ...db.models import AuditLog
from ...db.session import async_session
from ...identity import DEFAULT_PROMPT

logger = structlog.get_logger()
router = APIRouter()


def _templates(request: Request):
    return request.app.state.templates


def _identity_manager(request: Request):
    return getattr(request.app.state, "identity_manager", None)


# ---------------------------------------------------------------------------
# GET /identity
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
async def identity_page(request: Request):
    im = _identity_manager(request)
    prompt = DEFAULT_PROMPT
    compiled: dict = {}
    versions: list = []

    if im:
        prompt = await im.get_prompt()
        compiled = await im.get_compiled()
        versions = await im.list_versions()

    return _templates(request).TemplateResponse(
        request, "identity.html",
        {
            "prompt": prompt,
            "compiled": compiled,
            "versions": versions,
            "default_prompt": DEFAULT_PROMPT,
            "is_default": prompt.strip() == DEFAULT_PROMPT.strip(),
        },
    )


# ---------------------------------------------------------------------------
# POST /identity/save
# ---------------------------------------------------------------------------

@router.post("/save")
async def identity_save(request: Request):
    im = _identity_manager(request)
    if not im:
        return JSONResponse({"ok": False, "error": "Identity Engine not available"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        form = await request.form()
        body = dict(form)

    new_prompt = (body.get("prompt") or "").strip()
    if not new_prompt:
        return JSONResponse({"ok": False, "error": "Prompt cannot be empty"}, status_code=400)
    if len(new_prompt) > 6000:
        return JSONResponse({"ok": False, "error": "Prompt too long (max 6000 chars)"}, status_code=400)

    compiled = await im.save(new_prompt, source="dashboard")

    # Audit log
    try:
        async with async_session() as session:
            audit = AuditLog(
                id=str(uuid4()),
                event_type="identity.updated",
                source="dashboard",
                action="identity_save",
                input_summary=new_prompt[:200],
                output_summary="Identity updated via Dashboard",
                user_id="",
                chat_id="",
                latency_ms=0,
            )
            session.add(audit)
            await session.commit()
    except Exception:
        pass

    logger.info("identity.dashboard_save")
    return JSONResponse({"ok": True, "compiled": compiled})


# ---------------------------------------------------------------------------
# POST /identity/reset
# ---------------------------------------------------------------------------

@router.post("/reset")
async def identity_reset(request: Request):
    im = _identity_manager(request)
    if not im:
        return JSONResponse({"ok": False, "error": "Identity Engine not available"}, status_code=503)

    compiled = await im.reset(source="dashboard")

    try:
        async with async_session() as session:
            audit = AuditLog(
                id=str(uuid4()),
                event_type="identity.reset",
                source="dashboard",
                action="identity_reset",
                input_summary="reset to default",
                output_summary="Identity reset via Dashboard",
                user_id="",
                chat_id="",
                latency_ms=0,
            )
            session.add(audit)
            await session.commit()
    except Exception:
        pass

    logger.info("identity.dashboard_reset")
    return JSONResponse({"ok": True, "compiled": compiled, "prompt": DEFAULT_PROMPT})


# ---------------------------------------------------------------------------
# POST /identity/rollback
# ---------------------------------------------------------------------------

@router.post("/rollback")
async def identity_rollback(request: Request):
    im = _identity_manager(request)
    if not im:
        return JSONResponse({"ok": False, "error": "Identity Engine not available"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        form = await request.form()
        body = dict(form)

    ts_str = (body.get("ts") or "").strip()
    if not ts_str:
        return JSONResponse({"ok": False, "error": "Missing timestamp"}, status_code=400)

    compiled = await im.rollback(ts_str, source="dashboard")
    if compiled is None:
        return JSONResponse({"ok": False, "error": f"Version '{ts_str}' not found"}, status_code=404)

    prompt = await im.get_prompt()

    try:
        async with async_session() as session:
            audit = AuditLog(
                id=str(uuid4()),
                event_type="identity.rollback",
                source="dashboard",
                action="identity_rollback",
                input_summary=ts_str,
                output_summary="Identity rolled back via Dashboard",
                user_id="",
                chat_id="",
                latency_ms=0,
            )
            session.add(audit)
            await session.commit()
    except Exception:
        pass

    logger.info("identity.dashboard_rollback", ts=ts_str)
    return JSONResponse({"ok": True, "compiled": compiled, "prompt": prompt})
