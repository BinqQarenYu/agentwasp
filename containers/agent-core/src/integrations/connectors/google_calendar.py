"""Google Calendar connector — OAuth2 REST API.

Secrets (stored in vault — configure from dashboard Integrations page):
    client_id       — OAuth2 client ID from Google Cloud Console
    client_secret   — OAuth2 client secret from Google Cloud Console
    redirect_uri    — OAuth2 callback URL (e.g. https://agentwasp.com/integrations/google_calendar/oauth_callback)
    access_token    — OAuth2 access token (set automatically after OAuth flow)
    refresh_token   — OAuth2 refresh token (set automatically after OAuth flow)
    token_expires_at— Unix timestamp when access token expires (set automatically)

Setup:
    1. Go to https://console.cloud.google.com → APIs & Services → Credentials
    2. Create OAuth2 client (Web application type)
    3. Add redirect URI: https://YOUR_DOMAIN/integrations/google_calendar/oauth_callback
    4. In dashboard Integrations → Google Calendar → set client_id, client_secret, redirect_uri
    5. Click "Authorize with Google" button in the dashboard (or visit /integrations/google_calendar/oauth_start)
    6. Authorize → tokens saved automatically

Actions:
    list_events    — List upcoming calendar events                      (LOW)
    create_event   — Create a new calendar event                        (MEDIUM)
    update_event   — Update an existing calendar event                  (MEDIUM)
    delete_event   — Delete a calendar event                            (HIGH)
    get_event      — Fetch a specific event by ID                       (LOW)
    oauth_status   — Check OAuth2 authorization status                  (LOW)
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import httpx
import structlog

from ..base import (
    ActionSpec, BaseConnector, ConnectorManifest,
    ParamSpec, RateLimit, RiskLevel,
)

logger = structlog.get_logger()
_TIMEOUT = 20.0
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_CALENDAR_API = "https://www.googleapis.com/calendar/v3"


def _format_event(event: dict) -> dict:
    """Normalize a Google Calendar event dict for clean output."""
    start = event.get("start", {})
    end = event.get("end", {})
    return {
        "id": event.get("id", ""),
        "title": event.get("summary", "(no title)"),
        "start": start.get("dateTime", start.get("date", "")),
        "end": end.get("dateTime", end.get("date", "")),
        "location": event.get("location", ""),
        "description": event.get("description", ""),
        "status": event.get("status", ""),
        "html_link": event.get("htmlLink", ""),
    }


class GoogleCalendarConnector(BaseConnector):

    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            id="google-calendar",
            version="1.0.0",
            name="Google Calendar",
            category="productivity",
            description="Create, list, update and delete Google Calendar events via OAuth2.",
            capabilities=[
                "list_events",
                "create_events",
                "update_events",
                "delete_events",
            ],
            risk_level=RiskLevel.HIGH,
            required_secrets=["client_id", "client_secret", "redirect_uri"],
            config_schema={},
            rate_limits={
                "list_events":  RateLimit(requests_per_minute=60),
                "create_event": RateLimit(requests_per_minute=30),
                "update_event": RateLimit(requests_per_minute=30),
                "delete_event": RateLimit(requests_per_minute=20),
                "get_event":    RateLimit(requests_per_minute=60),
                "oauth_status": RateLimit(requests_per_minute=60),
            },
            actions=[
                ActionSpec(
                    id="list_events",
                    description="List upcoming Google Calendar events",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("days",        "integer", "Days ahead to look (default 7)",         required=False),
                        ParamSpec("max_results",  "integer", "Max events to return (default 10)",      required=False),
                        ParamSpec("calendar_id",  "string",  "Calendar ID (default: primary)",         required=False),
                    ],
                ),
                ActionSpec(
                    id="create_event",
                    description="Create a new Google Calendar event",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("title",       "string", "Event title",                              required=True),
                        ParamSpec("start",       "string", "Start datetime ISO8601 e.g. 2026-03-20T10:00:00-05:00", required=True),
                        ParamSpec("end",         "string", "End datetime ISO8601 (defaults start+1h)", required=False),
                        ParamSpec("description", "string", "Event description",                        required=False),
                        ParamSpec("location",    "string", "Event location",                           required=False),
                        ParamSpec("timezone",    "string", "Timezone e.g. America/New_York (default UTC)", required=False),
                        ParamSpec("calendar_id", "string", "Calendar ID (default: primary)",           required=False),
                    ],
                ),
                ActionSpec(
                    id="update_event",
                    description="Update an existing Google Calendar event",
                    risk_level=RiskLevel.MEDIUM, capability="controlled",
                    params=[
                        ParamSpec("event_id",    "string", "Event ID (from list_events)",              required=True),
                        ParamSpec("title",       "string", "New event title",                          required=False),
                        ParamSpec("start",       "string", "New start datetime ISO8601",               required=False),
                        ParamSpec("end",         "string", "New end datetime ISO8601",                 required=False),
                        ParamSpec("description", "string", "New description",                          required=False),
                        ParamSpec("location",    "string", "New location",                             required=False),
                        ParamSpec("timezone",    "string", "Timezone (default UTC)",                   required=False),
                        ParamSpec("calendar_id", "string", "Calendar ID (default: primary)",           required=False),
                    ],
                ),
                ActionSpec(
                    id="delete_event",
                    description="Delete a Google Calendar event",
                    risk_level=RiskLevel.HIGH, capability="controlled",
                    params=[
                        ParamSpec("event_id",    "string", "Event ID (from list_events)",              required=True),
                        ParamSpec("calendar_id", "string", "Calendar ID (default: primary)",           required=False),
                    ],
                ),
                ActionSpec(
                    id="get_event",
                    description="Fetch details of a specific calendar event",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[
                        ParamSpec("event_id",    "string", "Event ID",                                 required=True),
                        ParamSpec("calendar_id", "string", "Calendar ID (default: primary)",           required=False),
                    ],
                ),
                ActionSpec(
                    id="oauth_status",
                    description="Check Google Calendar OAuth2 authorization status",
                    risk_level=RiskLevel.LOW, capability="monitored",
                    params=[],
                ),
            ],
            homepage="https://calendar.google.com",
            docs_url="https://developers.google.com/calendar/api",
        )

    async def health_check(self, secrets: dict) -> bool:
        try:
            token = await self._get_valid_token(secrets)
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.get(
                    f"{_CALENDAR_API}/users/me/calendarList",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"maxResults": 1},
                )
            return r.status_code == 200
        except Exception:
            return False

    async def execute(self, action: str, params: dict, secrets: dict) -> dict:
        try:
            if action == "oauth_status":
                return await self._oauth_status(secrets)
            elif action == "list_events":
                return await self._list_events(params, secrets)
            elif action == "create_event":
                return await self._create_event(params, secrets)
            elif action == "update_event":
                return await self._update_event(params, secrets)
            elif action == "delete_event":
                return await self._delete_event(params, secrets)
            elif action == "get_event":
                return await self._get_event(params, secrets)
            else:
                return self.err(f"Unknown action: {action}")
        except _NotAuthorizedError as e:
            return self.err(str(e))
        except httpx.HTTPStatusError as e:
            body = e.response.text[:300]
            return self.err(f"Google API error {e.response.status_code}: {body}")
        except Exception as e:
            logger.error("google_calendar.execute_error", action=action, error=str(e))
            return self.err(str(e))

    # ── Internal helpers ────────────────────────────────────────────────────

    async def _get_valid_token(self, secrets: dict) -> str:
        """Return a valid access token, refreshing via vault if expired."""
        access_token = secrets.get("access_token", "")
        refresh_token = secrets.get("refresh_token", "")
        client_id = secrets.get("client_id", "")
        client_secret = secrets.get("client_secret", "")

        if not access_token and not refresh_token:
            raise _NotAuthorizedError(
                "Google Calendar not authorized. "
                "Go to Integrations → Google Calendar → click 'Authorize with Google'."
            )

        # Check expiry (allow 60s buffer)
        try:
            expires_at = float(secrets.get("token_expires_at", "0") or "0")
        except (ValueError, TypeError):
            expires_at = 0.0

        if time.time() < expires_at - 60:
            return access_token

        # Need to refresh
        if not refresh_token:
            raise _NotAuthorizedError(
                "Access token expired and no refresh token available. "
                "Re-authorize: Integrations → Google Calendar → 'Authorize with Google'."
            )
        if not client_id or not client_secret:
            raise _NotAuthorizedError("client_id and client_secret are required in vault.")

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(_TOKEN_URL, data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            })
            r.raise_for_status()
            data = r.json()

        new_token = data["access_token"]
        new_expires_at = time.time() + data.get("expires_in", 3600)

        # Persist refreshed token back to vault (fire-and-forget via registry if wired)
        try:
            vault = getattr(self, "_vault", None)
            if vault:
                await vault.set("google-calendar", "access_token", new_token)
                await vault.set("google-calendar", "token_expires_at", str(new_expires_at))
                if "refresh_token" in data:
                    await vault.set("google-calendar", "refresh_token", data["refresh_token"])
        except Exception:
            pass  # Non-fatal — token still valid for this request

        return new_token

    async def _oauth_status(self, secrets: dict) -> dict:
        client_id = secrets.get("client_id", "")
        client_secret = secrets.get("client_secret", "")
        access_token = secrets.get("access_token", "")
        refresh_token = secrets.get("refresh_token", "")
        redirect_uri = secrets.get("redirect_uri", "")

        has_creds = bool(client_id and client_secret)
        has_tokens = bool(access_token or refresh_token)

        try:
            expires_at = float(secrets.get("token_expires_at", "0") or "0")
        except (ValueError, TypeError):
            expires_at = 0.0

        is_expired = bool(access_token) and time.time() >= expires_at - 60

        if not has_creds:
            status = "NOT CONFIGURED — set client_id and client_secret in dashboard"
        elif not has_tokens:
            status = "CREDENTIALS SET — not yet authorized. Click 'Authorize with Google'"
        elif is_expired and not refresh_token:
            status = "TOKEN EXPIRED — no refresh token. Re-authorize required"
        elif is_expired:
            status = "AUTHORIZED — token will auto-refresh on next use"
        else:
            status = "AUTHORIZED — ready"

        return self.ok({
            "status": status,
            "has_client_credentials": has_creds,
            "has_access_token": bool(access_token),
            "has_refresh_token": bool(refresh_token),
            "has_redirect_uri": bool(redirect_uri),
            "token_expires_at": expires_at,
        })

    async def _list_events(self, params: dict, secrets: dict) -> dict:
        token = await self._get_valid_token(secrets)
        days = int(params.get("days", 7))
        max_results = int(params.get("max_results", 10))
        calendar_id = params.get("calendar_id", "primary")

        now = datetime.now(timezone.utc)
        time_min = now.isoformat()
        time_max = (now + timedelta(days=days)).isoformat()

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(
                f"{_CALENDAR_API}/calendars/{calendar_id}/events",
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "timeMin": time_min,
                    "timeMax": time_max,
                    "maxResults": max_results,
                    "singleEvents": "true",
                    "orderBy": "startTime",
                },
            )
            r.raise_for_status()
            data = r.json()

        events = [_format_event(e) for e in data.get("items", [])]
        return self.ok({"events": events, "count": len(events), "days_ahead": days})

    async def _create_event(self, params: dict, secrets: dict) -> dict:
        token = await self._get_valid_token(secrets)
        title = params.get("title", "")
        start = params.get("start", "")
        if not title:
            return self.err("'title' is required")
        if not start:
            return self.err("'start' (ISO8601 datetime) is required")

        end = params.get("end", "")
        if not end:
            try:
                end = (datetime.fromisoformat(start) + timedelta(hours=1)).isoformat()
            except Exception:
                end = start

        tz = params.get("timezone", "UTC")
        calendar_id = params.get("calendar_id", "primary")

        body: dict = {
            "summary": title,
            "start": {"dateTime": start, "timeZone": tz},
            "end": {"dateTime": end, "timeZone": tz},
        }
        if params.get("description"):
            body["description"] = params["description"]
        if params.get("location"):
            body["location"] = params["location"]

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(
                f"{_CALENDAR_API}/calendars/{calendar_id}/events",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=body,
            )
            r.raise_for_status()

        return self.ok({"event": _format_event(r.json()), "created": True})

    async def _update_event(self, params: dict, secrets: dict) -> dict:
        token = await self._get_valid_token(secrets)
        event_id = params.get("event_id", "")
        calendar_id = params.get("calendar_id", "primary")
        if not event_id:
            return self.err("'event_id' is required. Use list_events to find it.")

        # Fetch existing event
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(
                f"{_CALENDAR_API}/calendars/{calendar_id}/events/{event_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()
            event = r.json()

        tz = params.get("timezone", "UTC")
        if params.get("title"):
            event["summary"] = params["title"]
        if params.get("start"):
            event["start"] = {"dateTime": params["start"], "timeZone": tz}
        if params.get("end"):
            event["end"] = {"dateTime": params["end"], "timeZone": tz}
        if params.get("description"):
            event["description"] = params["description"]
        if params.get("location"):
            event["location"] = params["location"]

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.put(
                f"{_CALENDAR_API}/calendars/{calendar_id}/events/{event_id}",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=event,
            )
            r.raise_for_status()

        return self.ok({"event": _format_event(r.json()), "updated": True})

    async def _delete_event(self, params: dict, secrets: dict) -> dict:
        token = await self._get_valid_token(secrets)
        event_id = params.get("event_id", "")
        calendar_id = params.get("calendar_id", "primary")
        if not event_id:
            return self.err("'event_id' is required.")

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.delete(
                f"{_CALENDAR_API}/calendars/{calendar_id}/events/{event_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code == 404:
                return self.err(f"Event not found: {event_id}")
            r.raise_for_status()

        return self.ok({"deleted": True, "event_id": event_id})

    async def _get_event(self, params: dict, secrets: dict) -> dict:
        token = await self._get_valid_token(secrets)
        event_id = params.get("event_id", "")
        calendar_id = params.get("calendar_id", "primary")
        if not event_id:
            return self.err("'event_id' is required.")

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(
                f"{_CALENDAR_API}/calendars/{calendar_id}/events/{event_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code == 404:
                return self.err(f"Event not found: {event_id}")
            r.raise_for_status()

        return self.ok({"event": _format_event(r.json())})


class _NotAuthorizedError(Exception):
    pass
