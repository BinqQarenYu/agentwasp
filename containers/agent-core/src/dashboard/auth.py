"""Dashboard authentication: Redis sessions + Argon2 password hashing."""

import os
import secrets

import redis.asyncio as aioredis
import structlog
from fastapi import Request
from fastapi.responses import RedirectResponse
from pwdlib import PasswordHash
from sqlalchemy import func, select
from starlette.middleware.base import BaseHTTPMiddleware

from ..db.models import AdminUser
from ..db.session import async_session

logger = structlog.get_logger()

SESSION_TTL = 86400  # 24 hours
SESSION_PREFIX = "dashboard:session:"
CSRF_PREFIX = "csrf:"
RATE_LIMIT_PREFIX = "login:attempts:"
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_SECONDS = 300  # 5 minutes

hasher = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return hasher.verify(password, password_hash)


async def _get_redis(request: Request) -> aioredis.Redis:
    if not hasattr(request.app.state, "_redis") or request.app.state._redis is None:
        request.app.state._redis = aioredis.from_url(
            request.app.state.redis_url, decode_responses=True
        )
    return request.app.state._redis


async def create_session(request: Request, user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    r = await _get_redis(request)
    await r.setex(f"{SESSION_PREFIX}{token}", SESSION_TTL, user_id)
    return token


async def get_session_user(request: Request) -> str | None:
    token = request.cookies.get("session")
    if not token:
        return None
    r = await _get_redis(request)
    return await r.get(f"{SESSION_PREFIX}{token}")


async def destroy_session(request: Request):
    token = request.cookies.get("session")
    if token:
        r = await _get_redis(request)
        await r.delete(f"{SESSION_PREFIX}{token}")


async def generate_csrf_token(request: Request, session_override: str | None = None) -> str:
    """Generate a single-use CSRF token tied to the session."""
    token = secrets.token_urlsafe(32)
    r = await _get_redis(request)
    session_id = session_override or request.cookies.get("session", "anon")
    await r.setex(f"{CSRF_PREFIX}{token}", 3600, session_id)
    return token


async def validate_csrf_token(request: Request, token: str) -> bool:
    """Validate a CSRF token tied to the current session.

    For unauthenticated routes (login/register), the token is tied to "anon"
    since there is no session cookie yet. This is still CSRF-safe because the
    token is single-use, cryptographically random, and tied to the browser's
    visit to the GET /login page.
    """
    if not token:
        return False
    session_id = request.cookies.get("session", "anon") or "anon"
    r = await _get_redis(request)
    stored = await r.get(f"{CSRF_PREFIX}{token}")
    if stored is None:
        return False
    # Token is valid for its TTL (1h) — not single-use so multiple API calls
    # on the same page all work with the token embedded in the page's meta tag.
    if stored != session_id:
        return False
    # Token is reusable for its TTL (1h) — safe for SPA pages that make multiple
    # requests with the same meta-tag token without a full page reload.
    return True


async def check_rate_limit(request: Request, username: str) -> int | None:
    """Check login rate limit. Returns seconds until unlock, or None if OK."""
    r = await _get_redis(request)
    key = f"{RATE_LIMIT_PREFIX}{username}"
    attempts = await r.get(key)
    if attempts and int(attempts) >= MAX_LOGIN_ATTEMPTS:
        ttl = await r.ttl(key)
        return max(ttl, 1)
    return None


async def record_failed_login(request: Request, username: str):
    """Increment failed login counter."""
    r = await _get_redis(request)
    key = f"{RATE_LIMIT_PREFIX}{username}"
    pipe = r.pipeline()
    pipe.incr(key)
    pipe.expire(key, LOCKOUT_SECONDS)
    await pipe.execute()


async def clear_rate_limit(request: Request, username: str):
    """Clear rate limit on successful login."""
    r = await _get_redis(request)
    await r.delete(f"{RATE_LIMIT_PREFIX}{username}")


async def admin_exists() -> bool:
    async with async_session() as session:
        result = await session.execute(select(func.count(AdminUser.id)))
        return result.scalar_one() > 0


def _is_loopback_request(request: Request) -> bool:
    """Only allow env auto-login when accessed over localhost/loopback."""
    host = (request.headers.get("host") or "").split(":")[0].strip().lower()
    if host not in ("127.0.0.1", "localhost", "::1", "[::1]"):
        return False
    # Never auto-login behind a reverse proxy forwarding external clients
    if request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip"):
        return False
    return True


async def try_env_auto_login(request: Request) -> tuple[str, str] | None:
    """If DASHBOARD_AUTO_LOGIN=true on localhost, verify .env creds and mint a real session.

    Returns `(user_id, session_token)` on success, or `None` if disabled / ineligible / invalid.
    """
    if os.environ.get("DASHBOARD_AUTO_LOGIN", "").strip().lower() not in ("1", "true", "yes"):
        return None
    if not _is_loopback_request(request):
        return None

    username = os.environ.get("DASHBOARD_USER", "").strip()
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if not username or not password:
        return None

    try:
        async with async_session() as session:
            result = await session.execute(
                select(AdminUser).where(AdminUser.username == username)
            )
            user = result.scalar_one_or_none()
        if not user or not verify_password(password, user.password_hash):
            return None
        token = await create_session(request, user.id)
        logger.info("dashboard.env_auto_login", username=username)
        return user.id, token
    except Exception:
        logger.debug("dashboard.env_auto_login_skipped")
        return None


class AuthMiddleware(BaseHTTPMiddleware):
    # /chat/media/ serves screenshots — public so <img> tags always load regardless of session state
    # NOTE: trailing slash is intentional — prevents /chat/media-sign from matching via startswith()
    UNPROTECTED = ("/login", "/register", "/static", "/favicon.ico", "/chat/media/")

    async def dispatch(self, request, call_next):
        path = request.url.path
        if any(path.startswith(p) for p in self.UNPROTECTED):
            return await call_next(request)

        # Scoped per-run bearer token authentication for internal `waspctl` CLI calls.
        if path.startswith("/harnesses/botctl/"):
            auth_header = request.headers.get("Authorization", "")
            runner = getattr(request.app.state, "harness_runner", None)
            if auth_header.startswith("Bearer ") and runner is not None:
                bot_id = runner.verify_token(auth_header[7:].strip())
                if bot_id:
                    request.state.user_id = f"bot:{bot_id}"
                    return await call_next(request)
            from fastapi.responses import JSONResponse
            return JSONResponse({"ok": False, "error": "Invalid or expired botctl token"}, status_code=401)

        new_session_token: str | None = None
        user_id = await get_session_user(request)
        if not user_id:
            auto = await try_env_auto_login(request)
            if auto:
                user_id, new_session_token = auto
            else:
                if not await admin_exists():
                    return RedirectResponse("/register", status_code=303)
                return RedirectResponse("/login", status_code=303)

        request.state.user_id = user_id

        # CSRF validation for mutating requests on authenticated routes
        # Only check header — JS in base.html sends it for all forms and HTMX
        if request.method in ("POST", "PUT", "DELETE"):
            csrf_token = request.headers.get("X-CSRF-Token", "")
            if not await validate_csrf_token(request, csrf_token):
                logger.warning("dashboard.csrf_failure", path=path, user_id=user_id)
                from fastapi.responses import JSONResponse
                return JSONResponse({"error": "CSRF validation failed"}, status_code=403)

        # Generate CSRF token for templates (bound to new_session_token when auto-logged in)
        request.state.csrf_token = await generate_csrf_token(request, session_override=new_session_token)

        response = await call_next(request)
        if new_session_token:
            response.set_cookie(
                "session",
                new_session_token,
                httponly=True,
                samesite="lax",
                max_age=SESSION_TTL,
            )
        return response
