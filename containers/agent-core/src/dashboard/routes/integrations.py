"""Integration platform — dashboard routes."""

from __future__ import annotations

from pathlib import Path
import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()
logger = structlog.get_logger()

_LOGOS_DIR = Path(__file__).parent.parent / "static" / "logos"

# Map integration ID → SVG filename (without .svg)
_LOGO_MAP: dict[str, str] = {
    "slack":              "slack",
    "discord":            "discord",
    "github":             "github",
    "gmail-connector":    "gmail",
    "google-calendar":    "googlecalendar",
    "telegram":           "telegram",
    "whatsapp":           "whatsapp",
    "whatsapp-baileys":   "whatsapp-baileys",
    "spotify":            "spotify",
    "twitter":            "x",
    "teams":              "microsoftteams",
    "notion":             "notion",
    "trello":             "trello",
    "signal":             "signal",
    "zapier":             "zapier",
    "home-assistant":     "homeassistant",
    "webhook":            "webhook",
    "weather":            "openweathermap",
    "matrix":             "matrix",
    "obsidian":           "obsidian",
    "1password":          "1password",
    "sonos":              "sonos",
    "shazam":             "shazam",
    "philips-hue":        "philipshue",
    "eight-sleep":        "eightsleep",
    "nostr":              "nostr",
    "zalo":               "zalo",
    "nextcloud-talk":     "nextcloud",
    "mcp":                "anthropic",
    "browser-controlled": "googlechrome",
    "gif-search":         "giphy",
    "image-gen":          "openai",
    "email-generic":      "email",
    "bluebubbles":        "imessage",
    "cron":               "clockify",
    "webchat":            "googlechat",
    "platform-android":   "android",
    "platform-windows":   "windows",
    "platform-macos":     "macos",
    "platform-linux":     "linux",
    "platform-ios":       "ios",
}

def _load_logo_svgs() -> dict[str, str]:
    """Load all SVG logos as inline strings keyed by integration ID."""
    result: dict[str, str] = {}
    for intg_id, svg_name in _LOGO_MAP.items():
        path = _LOGOS_DIR / f"{svg_name}.svg"
        try:
            result[intg_id] = path.read_text(encoding="utf-8")
        except Exception:
            pass
    return result


def _registry(request: Request):
    """Get IntegrationRegistry from app state (may be None if not initialized)."""
    return getattr(request.app.state, "integration_registry", None)


@router.get("/", response_class=HTMLResponse)
async def integrations_page(request: Request):
    reg = _registry(request)
    integrations = reg.list_integrations() if reg else []
    return request.app.state.templates.TemplateResponse(request, "integrations.html", {
        "integrations": integrations,
        "registry_available": reg is not None,
        "logo_svgs": _load_logo_svgs(),
    })


@router.get("/api/list")
async def api_list(request: Request):
    reg = _registry(request)
    if not reg:
        return JSONResponse({"ok": False, "error": "Integration registry not available"})
    return JSONResponse({"ok": True, "integrations": reg.list_integrations()})


@router.get("/api/manifest/{integration_id}")
async def api_manifest(request: Request, integration_id: str):
    from ...integrations.base import IntegrationNotFoundError
    reg = _registry(request)
    if not reg:
        return JSONResponse({"ok": False, "error": "Integration registry not available"})
    try:
        m = reg.get_manifest(integration_id)
        # Build rich secret specs. Connectors that opt in expose `secret_specs`;
        # for legacy connectors we synthesize a bare entry per required_secrets
        # key so the frontend code path is uniform.
        _specs_raw = getattr(m, "secret_specs", None) or []
        if _specs_raw:
            _specs = [
                {
                    "key":         s.key,
                    "label":       s.label or s.key,
                    "help":        s.help,
                    "placeholder": s.placeholder,
                    "example":     s.example,
                    "required":    s.required,
                }
                for s in _specs_raw
            ]
        else:
            _specs = [
                {"key": k, "label": k, "help": "", "placeholder": "", "example": "", "required": True}
                for k in m.required_secrets
            ]
        return JSONResponse({"ok": True, "manifest": {
            "id":               m.id,
            "name":             m.name,
            "version":          m.version,
            "description":      m.description,
            "category":         m.category,
            "risk_level":       m.risk_level.value,
            "required_secrets": m.required_secrets,
            "secret_specs":     _specs,
            "capabilities":     m.capabilities,
            "homepage":         m.homepage,
            "docs_url":         m.docs_url,
            "actions":          [
                {
                    "id":          a.id,
                    "description": a.description,
                    "risk_level":  a.risk_level.value,
                    "capability":  a.capability,
                    "params":      [
                        {"name": p.name, "type": p.type, "required": p.required,
                         "description": p.description}
                        for p in a.params
                    ],
                }
                for a in m.actions
            ],
        }})
    except IntegrationNotFoundError:
        return JSONResponse({"ok": False, "error": f"Integration '{integration_id}' not found"}, status_code=404)


@router.post("/api/toggle")
async def api_toggle(request: Request):
    """Enable or disable an integration."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)
    reg = _registry(request)
    if not reg:
        return JSONResponse({"ok": False, "error": "Integration registry not available"})
    integration_id = (body.get("integration_id") or "").strip()
    if not integration_id:
        return JSONResponse({"ok": False, "error": "integration_id required"}, status_code=400)
    enabled = bool(body.get("enabled", True))
    try:
        if enabled:
            await reg.policy.enable(integration_id)
        else:
            await reg.policy.disable(integration_id)
        return JSONResponse({"ok": True, "integration_id": integration_id, "enabled": enabled})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.post("/api/reset_circuit_breaker")
async def api_reset_cb(request: Request):
    """Reset the circuit breaker for an integration."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)
    reg = _registry(request)
    if not reg:
        return JSONResponse({"ok": False, "error": "Integration registry not available"})
    integration_id = (body.get("integration_id") or "").strip()
    if not integration_id:
        return JSONResponse({"ok": False, "error": "integration_id required"}, status_code=400)
    cb = reg._breakers.get(integration_id)
    if not cb:
        return JSONResponse({"ok": False, "error": f"No circuit breaker for '{integration_id}'"}, status_code=404)
    cb.reset()
    return JSONResponse({"ok": True, "integration_id": integration_id, "state": cb.to_dict()})


@router.post("/api/set_secret")
async def api_set_secret(request: Request):
    """Store a secret for an integration in the vault."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)
    reg = _registry(request)
    if not reg:
        return JSONResponse({"ok": False, "error": "Integration registry not available"})
    integration_id = (body.get("integration_id") or "").strip()
    secret_key     = (body.get("key") or "").strip()
    secret_value   = (body.get("value") or "").strip()
    if not integration_id or not secret_key:
        return JSONResponse({"ok": False, "error": "integration_id and key are required"}, status_code=400)
    if not secret_value:
        return JSONResponse({"ok": False, "error": "value must not be empty"}, status_code=400)
    try:
        await reg.vault.set(integration_id, secret_key, secret_value)
        return JSONResponse({"ok": True, "integration_id": integration_id, "key": secret_key})
    except Exception as exc:
        logger.error("integrations.set_secret_failed", error=str(exc))
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.get("/api/health/{integration_id}")
async def api_health(request: Request, integration_id: str):
    """Trigger health check for a single integration."""
    import time
    reg = _registry(request)
    if not reg:
        return JSONResponse({"ok": False, "error": "Integration registry not available"})
    connector = reg._connectors.get(integration_id)
    if not connector:
        return JSONResponse({"ok": False, "error": f"Integration '{integration_id}' not found"}, status_code=404)
    try:
        healthy = await connector.health_check()
        return JSONResponse({"ok": True, "integration_id": integration_id,
                             "healthy": healthy, "timestamp": int(time.time())})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.post("/api/delete_secret")
async def api_delete_secret(request: Request):
    """Remove a secret for an integration from the vault."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)
    reg = _registry(request)
    if not reg:
        return JSONResponse({"ok": False, "error": "Integration registry not available"})
    integration_id = (body.get("integration_id") or "").strip()
    secret_key     = (body.get("key") or "").strip()
    if not integration_id or not secret_key:
        return JSONResponse({"ok": False, "error": "integration_id and key are required"}, status_code=400)
    try:
        await reg.vault.delete(integration_id, secret_key)
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.get("/api/secrets/{integration_id}")
async def api_list_secrets(request: Request, integration_id: str):
    """List which secret keys are configured for an integration (not values)."""
    reg = _registry(request)
    if not reg:
        return JSONResponse({"ok": False, "error": "Integration registry not available"})
    try:
        keys = await reg.vault.list_keys(integration_id)
        return JSONResponse({"ok": True, "integration_id": integration_id, "keys": keys})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.post("/api/test")
async def api_test(request: Request):
    """Execute an action for testing purposes."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)
    reg = _registry(request)
    if not reg:
        return JSONResponse({"ok": False, "error": "Integration registry not available"})
    integration_id = (body.get("integration_id") or "").strip()
    action         = (body.get("action") or "").strip()
    params         = body.get("params") or {}
    if not integration_id or not action:
        return JSONResponse({"ok": False, "error": "integration_id and action are required"}, status_code=400)
    try:
        result = await reg.execute(integration_id, action, params)
        return JSONResponse({"ok": True, "result": result})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.get("/api/failures/{integration_id}")
async def api_failures(request: Request, integration_id: str):
    """Fetch recent failure log entries for an integration from audit_log."""
    try:
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import select, desc, or_
        from ...db.models import AuditLog
        from ...db.session import async_session
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        async with async_session() as session:
            rows = (await session.execute(
                select(AuditLog)
                .where(AuditLog.timestamp >= cutoff)
                .where(
                    or_(
                        AuditLog.source.contains(integration_id),
                        AuditLog.event_type.contains(integration_id),
                        AuditLog.action.contains(integration_id),
                    )
                )
                .where(AuditLog.error.isnot(None))
                .order_by(desc(AuditLog.timestamp))
                .limit(30)
            )).scalars().all()
        return JSONResponse({"ok": True, "failures": [
            {
                "timestamp": r.timestamp.isoformat() if r.timestamp else "",
                "event_type": r.event_type or "",
                "action": r.action or "",
                "error": (r.error or "")[:300],
                "latency_ms": r.latency_ms,
            }
            for r in rows
        ]})
    except Exception as exc:
        logger.error("integrations.failures_error", error=str(exc))
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Google Calendar OAuth2 flow ─────────────────────────────────────────────

@router.get("/google_calendar/oauth_start")
async def google_calendar_oauth_start(request: Request):
    """Generate the Google OAuth2 authorization URL and redirect the browser to it."""
    from urllib.parse import urlencode
    from fastapi.responses import RedirectResponse

    reg = _registry(request)
    if not reg:
        return JSONResponse({"ok": False, "error": "Integration registry not available"})

    vault = getattr(reg, "_vault", None) or getattr(reg, "vault", None)
    if not vault:
        return JSONResponse({"ok": False, "error": "Vault not available"})

    try:
        client_id = await vault.get("google-calendar", "client_id") or ""
        client_secret = await vault.get("google-calendar", "client_secret") or ""
        redirect_uri = await vault.get("google-calendar", "redirect_uri") or ""
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Vault read error: {exc}"})

    if not client_id or not client_secret:
        from fastapi.responses import HTMLResponse as _HTML
        return _HTML("""
<!DOCTYPE html><html><head><title>Google Calendar Setup</title>
<style>body{font-family:sans-serif;max-width:520px;margin:60px auto;padding:0 20px}
.box{background:#fef3c7;border:1px solid #f59e0b;border-radius:8px;padding:20px;margin-top:16px}
h2{margin:0 0 8px}p{margin:4px 0;font-size:14px}a{color:#6366f1}ol{padding-left:20px;font-size:14px;line-height:2}</style>
</head><body>
<h2>⚠️ Credentials missing</h2>
<div class="box">
<p>Save <b>client_id</b>, <b>client_secret</b> and <b>redirect_uri</b> in the dashboard first:</p>
<ol>
<li>Go to <a href="/integrations" target="_blank">Integrations</a></li>
<li>Find <b>Google Calendar</b> and click Configure</li>
<li>Enter and save each credential in its labeled field</li>
<li>Come back here and click Authorize again</li>
</ol>
</div>
<p style="margin-top:20px"><a href="/integrations">← Back to Integrations</a></p>
</body></html>""")
    if not redirect_uri:
        # Default: this dashboard's own callback URL
        base_url = str(request.base_url).rstrip("/")
        redirect_uri = f"{base_url}/integrations/google_calendar/oauth_callback"

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/calendar",
        "access_type": "offline",
        "prompt": "consent",
    }
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    return RedirectResponse(url=auth_url)


@router.get("/google_calendar/oauth_callback")
async def google_calendar_oauth_callback(request: Request, code: str = "", error: str = ""):
    """Handle Google OAuth2 redirect. Exchanges code for tokens and saves to vault."""
    import time
    import httpx as _httpx
    from fastapi.responses import HTMLResponse as _HTML

    if error:
        return _HTML(
            f"<h2>Authorization denied</h2><p>{error}</p>"
            "<p><a href='/integrations'>Back to Integrations</a></p>"
        )
    if not code:
        return _HTML(
            "<h2>No authorization code received</h2>"
            "<p><a href='/integrations'>Back to Integrations</a></p>"
        )

    reg = _registry(request)
    vault = getattr(reg, "_vault", None) or getattr(reg, "vault", None)
    if not vault:
        return _HTML("<h2>Vault not available</h2>")

    try:
        client_id = await vault.get("google-calendar", "client_id") or ""
        client_secret = await vault.get("google-calendar", "client_secret") or ""
        redirect_uri = await vault.get("google-calendar", "redirect_uri") or ""
    except Exception as exc:
        return _HTML(f"<h2>Vault error: {exc}</h2>")

    if not redirect_uri:
        base_url = str(request.base_url).rstrip("/")
        redirect_uri = f"{base_url}/integrations/google_calendar/oauth_callback"

    try:
        async with _httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post("https://oauth2.googleapis.com/token", data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            })
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        return _HTML(f"<h2>Token exchange failed</h2><p>{exc}</p>")

    access_token = data.get("access_token", "")
    refresh_token = data.get("refresh_token", "")
    expires_in = data.get("expires_in", 3600)
    expires_at = time.time() + expires_in

    await vault.set("google-calendar", "access_token", access_token)
    await vault.set("google-calendar", "token_expires_at", str(expires_at))
    if refresh_token:
        await vault.set("google-calendar", "refresh_token", refresh_token)

    logger.info("google_calendar.oauth_tokens_saved", has_refresh=bool(refresh_token))

    return _HTML("""
<!DOCTYPE html>
<html>
<head><title>Google Calendar Authorized</title>
<style>
  body { font-family: sans-serif; max-width: 500px; margin: 80px auto; text-align: center; }
  .ok { color: #22c55e; font-size: 48px; }
  h2 { margin-top: 12px; }
  a { display: inline-block; margin-top: 24px; padding: 10px 20px;
      background: #6366f1; color: white; border-radius: 6px; text-decoration: none; }
</style>
</head>
<body>
  <div class="ok">✓</div>
  <h2>Google Calendar authorized!</h2>
  <p>Tokens saved. You can now use Google Calendar from the agent.</p>
  <a href="/integrations">Back to Integrations</a>
</body>
</html>
""")
