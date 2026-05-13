"""Login, register, and logout routes."""

from uuid import uuid4

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from ..auth import (
    admin_exists, create_session, destroy_session, hash_password, verify_password,
    generate_csrf_token, validate_csrf_token,
    check_rate_limit, record_failed_login, clear_rate_limit,
)
from ...db.models import AdminUser, AuditLog
from ...db.session import async_session

logger = structlog.get_logger()
router = APIRouter()


async def _audit(event_type: str, action: str, username: str = "", error: str = ""):
    """Write a security audit log entry."""
    try:
        async with async_session() as session:
            session.add(AuditLog(
                id=str(uuid4()),
                event_type=event_type,
                source="dashboard",
                action=action,
                input_summary=f"username={username}" if username else "",
                error=error or None,
            ))
            await session.commit()
    except Exception:
        logger.exception("audit.write_failed")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if not await admin_exists():
        return RedirectResponse("/register", status_code=303)
    csrf = await generate_csrf_token(request)
    return request.app.state.templates.TemplateResponse(
        request, "login.html", {"error": "", "csrf_token": csrf}
    )


@router.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username = form.get("username", "").strip()
    password = form.get("password", "")
    csrf = form.get("csrf_token", "")

    # CSRF check
    if not await validate_csrf_token(request, csrf):
        logger.warning("dashboard.login_csrf_failure")
        new_csrf = await generate_csrf_token(request)
        return request.app.state.templates.TemplateResponse(
            request, "login.html",
            {"error": "Session expired. Please try again.", "csrf_token": new_csrf},
            status_code=403,
        )

    # Rate limit check
    lockout = await check_rate_limit(request, username)
    if lockout:
        logger.warning("dashboard.login_blocked", username=username, lockout_seconds=lockout)
        await _audit("security.login_blocked", "login_attempt", username, f"Rate limited ({lockout}s)")
        new_csrf = await generate_csrf_token(request)
        return request.app.state.templates.TemplateResponse(
            request, "login.html",
            {"error": f"Too many failed attempts. Try again in {lockout}s.", "csrf_token": new_csrf},
            status_code=429,
        )

    async with async_session() as session:
        result = await session.execute(
            select(AdminUser).where(AdminUser.username == username)
        )
        user = result.scalar_one_or_none()

    if not user or not verify_password(password, user.password_hash):
        await record_failed_login(request, username)
        logger.warning("dashboard.login_failed", username=username)
        await _audit("security.login_failed", "login_attempt", username, "Invalid credentials")
        new_csrf = await generate_csrf_token(request)
        return request.app.state.templates.TemplateResponse(
            request, "login.html",
            {"error": "Invalid username or password.", "csrf_token": new_csrf},
            status_code=401,
        )

    await clear_rate_limit(request, username)
    token = await create_session(request, user.id)
    response = RedirectResponse("/overview", status_code=303)
    response.set_cookie("session", token, httponly=True, samesite="lax", max_age=86400)
    logger.info("dashboard.login", username=username)
    await _audit("security.login_success", "login", username)
    return response


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    if await admin_exists():
        return RedirectResponse("/login", status_code=303)
    csrf = await generate_csrf_token(request)
    return request.app.state.templates.TemplateResponse(
        request, "register.html", {"error": "", "csrf_token": csrf}
    )


@router.post("/register")
async def register_submit(request: Request):
    if await admin_exists():
        return RedirectResponse("/login", status_code=303)

    form = await request.form()
    username = form.get("username", "").strip()
    password = form.get("password", "")
    csrf = form.get("csrf_token", "")

    # CSRF check
    if not await validate_csrf_token(request, csrf):
        new_csrf = await generate_csrf_token(request)
        return request.app.state.templates.TemplateResponse(
            request, "register.html",
            {"error": "Session expired. Please try again.", "csrf_token": new_csrf},
            status_code=403,
        )

    if len(username) < 3 or len(password) < 8:
        new_csrf = await generate_csrf_token(request)
        return request.app.state.templates.TemplateResponse(
            request, "register.html",
            {"error": "Username 3+ chars, password 8+ chars.", "csrf_token": new_csrf},
            status_code=400,
        )

    user = AdminUser(
        id=str(uuid4()),
        username=username,
        password_hash=hash_password(password),
    )
    async with async_session() as session:
        session.add(user)
        await session.commit()

    token = await create_session(request, user.id)
    response = RedirectResponse("/overview", status_code=303)
    response.set_cookie("session", token, httponly=True, samesite="lax", max_age=86400)
    logger.info("dashboard.admin_created", username=username)
    await _audit("security.register", "admin_created", username)
    return response


@router.get("/logout")
async def logout(request: Request):
    await destroy_session(request)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("session")
    return response
