"""Google Calendar skill — full-featured calendar management via Google Calendar API v3.

Credentials stored in integration vault (Dashboard → Integrations → Google Calendar).

Actions:
    status          — Check authorization status
    list_events     — List upcoming events (with attendees, reminders, recurrence info)
    search_events   — Search events by text query
    get_event       — Get full details of a specific event
    create_event    — Create event (with attendees, reminders, recurrence, all-day, location, color)
    update_event    — Update any field of an existing event
    delete_event    — Delete an event
    list_calendars  — List all calendars in the account
    quick_add       — Create event from natural language string (uses Google's own parser)
"""

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from ..base import SkillBase
from ..types import ParamType, SkillDefinition, SkillParam, SkillResult

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_CALENDAR_API = "https://www.googleapis.com/calendar/v3"
_DEFAULT_TZ = os.environ.get("TIMEZONE", "America/Santiago")

# Google Calendar event colors
_COLORS = {
    "tomato": "1", "flamingo": "2", "tangerine": "3", "banana": "4",
    "sage": "5", "basil": "6", "peacock": "7", "blueberry": "8",
    "lavender": "9", "grape": "10", "graphite": "11",
    "red": "1", "pink": "2", "orange": "3", "yellow": "4",
    "green": "6", "teal": "7", "blue": "8", "purple": "10", "gray": "11",
}


def _now_local() -> datetime:
    return datetime.now(ZoneInfo(_DEFAULT_TZ))


def _resolve_datetime(value: str, tz_name: str = "") -> str:
    """Convert natural-language or partial datetime strings to ISO8601 with timezone."""
    tz = ZoneInfo(tz_name or _DEFAULT_TZ)
    now = datetime.now(tz)
    v = value.strip()

    # Full ISO8601 with T
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", v):
        if not re.search(r"[+\-]\d{2}:?\d{2}$|Z$", v):
            dt = datetime.fromisoformat(v).replace(tzinfo=tz)
            return dt.isoformat()
        return v

    # "YYYY-MM-DD HH:MM"
    m = re.match(r"^(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})(?::\d{2})?$", v)
    if m:
        dt = datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}:00").replace(tzinfo=tz)
        return dt.isoformat()

    # Natural language day words
    day_map = {
        "hoy": 0, "today": 0,
        "mañana": 1, "manana": 1, "tomorrow": 1,
        "pasado mañana": 2, "pasado manana": 2, "day after tomorrow": 2,
    }
    for word, delta in sorted(day_map.items(), key=lambda x: -len(x[0])):
        pattern = re.compile(
            r"^" + re.escape(word) + r"\s+(?:a\s+las?\s+)?(\d{1,2})(?::(\d{2}))?(?::\d{2})?\s*(am|pm|hrs?|h)?$",
            re.IGNORECASE,
        )
        mo = pattern.match(v)
        if mo:
            hour = int(mo.group(1))
            minute = int(mo.group(2) or 0)
            ampm = (mo.group(3) or "").lower()
            if ampm == "pm" and hour < 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0
            base = now + timedelta(days=delta)
            dt = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
            return dt.isoformat()

    # "HH:MM" alone → today
    m = re.match(r"^(\d{1,2}):(\d{2})(?::\d{2})?$", v)
    if m:
        dt = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        return dt.isoformat()

    # "18h", "6pm", "18hs"
    m = re.match(r"^(\d{1,2})\s*(am|pm|h|hs|hrs?)?$", v, re.IGNORECASE)
    if m:
        hour = int(m.group(1))
        ampm = (m.group(2) or "").lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        dt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        return dt.isoformat()

    # Try fromisoformat as last resort
    try:
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.isoformat()
    except ValueError:
        pass

    return v


def _build_reminders(reminders_str: str) -> dict:
    """
    Parse reminder string into Google API reminders object.
    Examples:
      "30"            → email 30min before
      "popup:10"      → popup 10min before
      "email:30"      → email 30min before
      "popup:10,email:60" → both
      "none"          → no reminders
    """
    if not reminders_str or reminders_str.lower() == "none":
        return {"useDefault": False, "overrides": []}

    overrides = []
    for part in reminders_str.split(","):
        part = part.strip()
        if ":" in part:
            method, mins = part.split(":", 1)
            method = method.strip().lower()
            if method not in ("email", "popup"):
                method = "popup"
        else:
            method = "popup"
            mins = part
        try:
            overrides.append({"method": method, "minutes": int(mins.strip())})
        except ValueError:
            pass

    if not overrides:
        return {"useDefault": True}
    return {"useDefault": False, "overrides": overrides}


def _build_attendees(attendees_str: str) -> list:
    """
    Parse attendee string into Google API attendees list.
    Examples:
      "user@example.com"
      "user@example.com,bob@example.com"
    """
    if not attendees_str:
        return []
    emails = [e.strip() for e in attendees_str.split(",") if "@" in e.strip()]
    return [{"email": e} for e in emails]


def _build_recurrence(recurrence_str: str) -> list:
    """
    Convert human-readable recurrence to RRULE.
    Examples:
      "daily"           → RRULE:FREQ=DAILY
      "weekly"          → RRULE:FREQ=WEEKLY
      "monthly"         → RRULE:FREQ=MONTHLY
      "weekdays"        → RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR
      "daily count=5"   → RRULE:FREQ=DAILY;COUNT=5
      "weekly until=2026-12-31" → RRULE:FREQ=WEEKLY;UNTIL=20261231T000000Z
      "RRULE:FREQ=..."  → passed through as-is
    """
    if not recurrence_str:
        return []
    r = recurrence_str.strip()
    if r.upper().startswith("RRULE:"):
        return [r.upper()]

    rl = r.lower()
    freq = "DAILY"
    if "week" in rl:
        freq = "WEEKLY"
    elif "month" in rl:
        freq = "MONTHLY"
    elif "year" in rl:
        freq = "YEARLY"
    elif "weekday" in rl or "laboral" in rl or "hábil" in rl:
        return ["RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"]

    parts = [f"RRULE:FREQ={freq}"]
    count_m = re.search(r"count\s*=\s*(\d+)", rl)
    if count_m:
        parts = [f"RRULE:FREQ={freq};COUNT={count_m.group(1)}"]
    until_m = re.search(r"until\s*=\s*([\d\-]+)", rl)
    if until_m:
        until_date = until_m.group(1).replace("-", "") + "T000000Z"
        parts = [f"RRULE:FREQ={freq};UNTIL={until_date}"]

    return parts


def _format_event_full(e: dict) -> str:
    """Format event for detailed display."""
    lines = []
    lines.append(f"Title: {e.get('summary', '(no title)')}")
    lines.append(f"ID: {e.get('id', '')}")

    start = e.get("start", {})
    end = e.get("end", {})
    start_str = start.get("dateTime", start.get("date", "?"))
    end_str = end.get("dateTime", end.get("date", "?"))
    lines.append(f"Start: {start_str}")
    lines.append(f"End: {end_str}")

    if e.get("location"):
        lines.append(f"Location: {e['location']}")
    if e.get("description"):
        lines.append(f"Description: {e['description'][:200]}")

    attendees = e.get("attendees", [])
    if attendees:
        att_list = ", ".join(
            f"{a.get('displayName', a.get('email', '?'))} ({a.get('responseStatus', '?')})"
            for a in attendees
        )
        lines.append(f"Attendees: {att_list}")

    reminders = e.get("reminders", {})
    if reminders.get("overrides"):
        rem_list = ", ".join(
            f"{r['method']} {r['minutes']}min before" for r in reminders["overrides"]
        )
        lines.append(f"Reminders: {rem_list}")
    elif reminders.get("useDefault"):
        lines.append("Reminders: default")

    recurrence = e.get("recurrence", [])
    if recurrence:
        lines.append(f"Recurrence: {'; '.join(recurrence)}")

    if e.get("htmlLink"):
        lines.append(f"Link: {e['htmlLink']}")

    return "\n".join(lines)


class GoogleCalendarSkill(SkillBase):
    """Full-featured Google Calendar skill — reads credentials from integration vault."""

    def __init__(self, vault=None):
        self._vault = vault

    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="google_calendar",
            description=(
                "Full Google Calendar management. Actions: status, list_events, search_events, "
                "get_event, create_event, update_event, delete_event, list_calendars, quick_add. "
                "For start/end use 'hoy HH:MM', 'mañana HH:MM', or 'YYYY-MM-DD HH:MM' — "
                "the skill resolves today/tomorrow automatically. "
                "Supports reminders (popup/email), attendees, recurrence, all-day events, location, color."
            ),
            params=[
                SkillParam(name="action",      param_type=ParamType.STRING,  description="status | list_events | search_events | get_event | create_event | update_event | delete_event | list_calendars | quick_add"),
                SkillParam(name="title",       param_type=ParamType.STRING,  description="Event title", required=False),
                SkillParam(name="start",       param_type=ParamType.STRING,  description="Start: 'hoy 18:00', 'mañana 10:00', 'YYYY-MM-DD HH:MM', or full ISO8601", required=False),
                SkillParam(name="end",         param_type=ParamType.STRING,  description="End time (same formats, defaults start+1h)", required=False),
                SkillParam(name="all_day",     param_type=ParamType.STRING,  description="Date for all-day event e.g. '2026-03-20' or 'hoy'", required=False),
                SkillParam(name="description", param_type=ParamType.STRING,  description="Event description/notes", required=False),
                SkillParam(name="location",    param_type=ParamType.STRING,  description="Event location or address", required=False),
                SkillParam(name="attendees",   param_type=ParamType.STRING,  description="Comma-separated emails to invite e.g. 'alice@example.com,bob@example.com'", required=False),
                SkillParam(name="reminders",   param_type=ParamType.STRING,  description="Reminder spec: 'popup:10,email:60' or '30' (minutes before) or 'none'", required=False),
                SkillParam(name="recurrence",  param_type=ParamType.STRING,  description="Recurrence: 'daily', 'weekly', 'monthly', 'weekdays', 'daily count=5', 'weekly until=2026-12-31'", required=False),
                SkillParam(name="color",       param_type=ParamType.STRING,  description="Event color: red, blue, green, yellow, purple, pink, orange, teal, gray", required=False),
                SkillParam(name="event_id",    param_type=ParamType.STRING,  description="Event ID (from list_events or search_events)", required=False),
                SkillParam(name="query",       param_type=ParamType.STRING,  description="Search text (for search_events)", required=False),
                SkillParam(name="text",        param_type=ParamType.STRING,  description="Natural language event string for quick_add e.g. 'Dentist tomorrow at 10am'", required=False),
                SkillParam(name="days",        param_type=ParamType.INTEGER, description="Days ahead to list (default 7)", required=False),
                SkillParam(name="max_results", param_type=ParamType.INTEGER, description="Max events to return (default 15)", required=False),
                SkillParam(name="calendar_id", param_type=ParamType.STRING,  description="Calendar ID (default: primary)", required=False),
                SkillParam(name="timezone",    param_type=ParamType.STRING,  description="Timezone override e.g. America/New_York", required=False),
                SkillParam(name="send_updates",param_type=ParamType.STRING,  description="Notify attendees: 'all' (default) | 'externalOnly' | 'none'", required=False),
            ],
            category="productivity",
            timeout_seconds=30.0,
            cooldown_seconds=1.0,
        )

    async def _get_secrets(self) -> dict:
        if not self._vault:
            return {}
        result = {}
        for k in ["client_id", "client_secret", "access_token", "refresh_token", "token_expires_at"]:
            try:
                v = await self._vault.get("google_calendar", k)
                if v:
                    result[k] = v
            except Exception:
                pass
        return result

    async def _get_valid_token(self, secrets: dict) -> str:
        access_token = secrets.get("access_token", "")
        refresh_token = secrets.get("refresh_token", "")
        client_id = secrets.get("client_id", "")
        client_secret = secrets.get("client_secret", "")

        if not access_token and not refresh_token:
            raise ValueError(
                "Google Calendar not authorized. "
                "Go to Dashboard → Integrations → Google Calendar → 'Authorize with Google'."
            )

        try:
            expires_at = float(secrets.get("token_expires_at", "0") or "0")
        except Exception:
            expires_at = 0.0

        if time.time() < expires_at - 60:
            return access_token

        if not refresh_token:
            raise ValueError("Access token expired. Re-authorize in Integrations → Google Calendar.")

        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(_TOKEN_URL, data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            })
            r.raise_for_status()
            data = r.json()

        new_token = data["access_token"]
        new_expires = time.time() + data.get("expires_in", 3600)

        if self._vault:
            try:
                await self._vault.set("google_calendar", "access_token", new_token)
                await self._vault.set("google_calendar", "token_expires_at", str(new_expires))
                if "refresh_token" in data:
                    await self._vault.set("google_calendar", "refresh_token", data["refresh_token"])
            except Exception:
                pass

        return new_token

    async def execute(self, action: str = "status", **kwargs) -> SkillResult:
        try:
            action = action.lower().strip()
            secrets = await self._get_secrets()

            dispatch = {
                "status":         self._status,
                "list_events":    self._list_events,
                "search_events":  self._search_events,
                "get_event":      self._get_event,
                "create_event":   self._create_event,
                "update_event":   self._update_event,
                "delete_event":   self._delete_event,
                "list_calendars": self._list_calendars,
                "quick_add":      self._quick_add,
            }
            if action not in dispatch:
                return SkillResult(
                    skill_name="google_calendar", success=False, output="",
                    error=f"Unknown action '{action}'. Use: {', '.join(dispatch.keys())}",
                )
            return await dispatch[action](secrets, **kwargs)
        except Exception as e:
            return SkillResult(skill_name="google_calendar", success=False, output="", error=str(e))

    # ── Actions ────────────────────────────────────────────────────────────────

    async def _status(self, secrets: dict, **_) -> SkillResult:
        has_creds = bool(secrets.get("client_id") and secrets.get("client_secret"))
        has_token = bool(secrets.get("access_token") or secrets.get("refresh_token"))
        try:
            expires_at = float(secrets.get("token_expires_at", "0") or "0")
        except Exception:
            expires_at = 0.0

        if not has_creds:
            msg = "NOT CONFIGURED"
        elif not has_token:
            msg = "CREDENTIALS SET but not authorized — click 'Authorize with Google' in Integrations"
        elif time.time() >= expires_at - 60 and not secrets.get("refresh_token"):
            msg = "TOKEN EXPIRED — re-authorize in Integrations → Google Calendar"
        else:
            msg = f"AUTHORIZED and ready (timezone: {_DEFAULT_TZ})"

        return SkillResult(skill_name="google_calendar", success=True,
                           output=f"Google Calendar status: {msg}")

    async def _list_events(self, secrets: dict, days: int = 7, max_results: int = 15,
                           calendar_id: str = "primary", **kwargs) -> SkillResult:
        token = await self._get_valid_token(secrets)
        now = _now_local()
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(
                f"{_CALENDAR_API}/calendars/{calendar_id}/events",
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "timeMin": now.isoformat(),
                    "timeMax": (now + timedelta(days=int(days))).isoformat(),
                    "maxResults": int(max_results),
                    "singleEvents": "true",
                    "orderBy": "startTime",
                },
            )
            r.raise_for_status()
            data = r.json()

        events = data.get("items", [])
        if not events:
            return SkillResult(skill_name="google_calendar", success=True,
                               output=f"No events in the next {days} days.")

        lines = [f"Events in the next {days} days ({len(events)} found):"]
        for e in events:
            start = e.get("start", {})
            start_str = start.get("dateTime", start.get("date", "?"))
            attendees = e.get("attendees", [])
            att = f" [{len(attendees)} attendees]" if attendees else ""
            rec = " [recurring]" if e.get("recurrence") else ""
            lines.append(f"• {e.get('summary', '(no title)')} — {start_str}{att}{rec} [ID: {e.get('id', '')}]")
        return SkillResult(skill_name="google_calendar", success=True, output="\n".join(lines))

    async def _search_events(self, secrets: dict, query: str = "", days: int = 30,
                             max_results: int = 10, calendar_id: str = "primary", **kwargs) -> SkillResult:
        if not query:
            return SkillResult(skill_name="google_calendar", success=False, output="",
                               error="'query' is required for search_events")
        token = await self._get_valid_token(secrets)
        now = _now_local()
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(
                f"{_CALENDAR_API}/calendars/{calendar_id}/events",
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "q": query,
                    "timeMin": now.isoformat(),
                    "timeMax": (now + timedelta(days=int(days))).isoformat(),
                    "maxResults": int(max_results),
                    "singleEvents": "true",
                    "orderBy": "startTime",
                },
            )
            r.raise_for_status()
            data = r.json()

        events = data.get("items", [])
        if not events:
            return SkillResult(skill_name="google_calendar", success=True,
                               output=f"No events found matching '{query}'.")

        lines = [f"Search results for '{query}' ({len(events)} found):"]
        for e in events:
            start = e.get("start", {})
            start_str = start.get("dateTime", start.get("date", "?"))
            lines.append(f"• {e.get('summary', '(no title)')} — {start_str} [ID: {e.get('id', '')}]")
        return SkillResult(skill_name="google_calendar", success=True, output="\n".join(lines))

    async def _get_event(self, secrets: dict, event_id: str = "",
                         calendar_id: str = "primary", **kwargs) -> SkillResult:
        if not event_id:
            return SkillResult(skill_name="google_calendar", success=False, output="",
                               error="'event_id' is required.")
        token = await self._get_valid_token(secrets)
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(
                f"{_CALENDAR_API}/calendars/{calendar_id}/events/{event_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code == 404:
                return SkillResult(skill_name="google_calendar", success=False, output="",
                                   error=f"Event not found: {event_id}. Use list_events to get valid IDs.")
            r.raise_for_status()
        return SkillResult(skill_name="google_calendar", success=True,
                           output=_format_event_full(r.json()))

    async def _create_event(self, secrets: dict, title: str = "", start: str = "",
                            end: str = "", all_day: str = "", description: str = "",
                            location: str = "", attendees: str = "", reminders: str = "",
                            recurrence: str = "", color: str = "", timezone: str = "",
                            calendar_id: str = "primary", send_updates: str = "all",
                            **kwargs) -> SkillResult:
        if not title:
            return SkillResult(skill_name="google_calendar", success=False, output="",
                               error="'title' is required")

        tz_name = timezone or _DEFAULT_TZ
        token = await self._get_valid_token(secrets)

        body: dict = {"summary": title}

        # All-day vs timed event
        if all_day:
            # Resolve date
            if all_day.lower() in ("hoy", "today"):
                date_str = _now_local().strftime("%Y-%m-%d")
            elif all_day.lower() in ("mañana", "manana", "tomorrow"):
                date_str = (_now_local() + timedelta(days=1)).strftime("%Y-%m-%d")
            else:
                date_str = all_day[:10]  # take YYYY-MM-DD part
            body["start"] = {"date": date_str}
            body["end"] = {"date": date_str}
        elif start:
            start_iso = _resolve_datetime(start, tz_name)
            if not end:
                try:
                    end_iso = (datetime.fromisoformat(start_iso) + timedelta(hours=1)).isoformat()
                except Exception:
                    end_iso = start_iso
            else:
                end_iso = _resolve_datetime(end, tz_name)
            body["start"] = {"dateTime": start_iso, "timeZone": tz_name}
            body["end"] = {"dateTime": end_iso, "timeZone": tz_name}
        else:
            return SkillResult(skill_name="google_calendar", success=False, output="",
                               error="Either 'start' or 'all_day' is required")

        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if attendees:
            body["attendees"] = _build_attendees(attendees)
        if reminders:
            body["reminders"] = _build_reminders(reminders)
        if recurrence:
            body["recurrence"] = _build_recurrence(recurrence)
        if color:
            color_id = _COLORS.get(color.lower(), "")
            if color_id:
                body["colorId"] = color_id

        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                f"{_CALENDAR_API}/calendars/{calendar_id}/events",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                params={"sendUpdates": send_updates},
                json=body,
            )
            r.raise_for_status()
            event = r.json()

        start_out = event.get("start", {}).get("dateTime", event.get("start", {}).get("date", ""))
        att_count = len(event.get("attendees", []))
        att_note = f" — {att_count} attendee(s) notified" if att_count else ""
        return SkillResult(skill_name="google_calendar", success=True,
                           output=f"Event created: '{title}' at {start_out}{att_note} [ID: {event.get('id', '')}]")

    async def _update_event(self, secrets: dict, event_id: str = "", title: str = "",
                            start: str = "", end: str = "", description: str = "",
                            location: str = "", attendees: str = "", reminders: str = "",
                            recurrence: str = "", color: str = "", timezone: str = "",
                            calendar_id: str = "primary", send_updates: str = "all",
                            **kwargs) -> SkillResult:
        if not event_id:
            return SkillResult(skill_name="google_calendar", success=False, output="",
                               error="'event_id' is required. Use list_events or search_events to find it.")

        tz_name = timezone or _DEFAULT_TZ
        token = await self._get_valid_token(secrets)

        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(
                f"{_CALENDAR_API}/calendars/{calendar_id}/events/{event_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code == 404:
                return SkillResult(skill_name="google_calendar", success=False, output="",
                                   error=f"Event not found with ID '{event_id}'. Use list_events to get correct IDs.")
            r.raise_for_status()
            event = r.json()

        if title:
            event["summary"] = title
        if start:
            event["start"] = {"dateTime": _resolve_datetime(start, tz_name), "timeZone": tz_name}
        if end:
            event["end"] = {"dateTime": _resolve_datetime(end, tz_name), "timeZone": tz_name}
        if description:
            event["description"] = description
        if location:
            event["location"] = location
        if attendees:
            # Merge with existing attendees
            existing = {a["email"]: a for a in event.get("attendees", [])}
            for a in _build_attendees(attendees):
                existing[a["email"]] = a
            event["attendees"] = list(existing.values())
        if reminders:
            event["reminders"] = _build_reminders(reminders)
        if recurrence:
            event["recurrence"] = _build_recurrence(recurrence)
        if color:
            color_id = _COLORS.get(color.lower(), "")
            if color_id:
                event["colorId"] = color_id

        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.put(
                f"{_CALENDAR_API}/calendars/{calendar_id}/events/{event_id}",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                params={"sendUpdates": send_updates},
                json=event,
            )
            r.raise_for_status()
            updated = r.json()

        start_out = updated.get("start", {}).get("dateTime", updated.get("start", {}).get("date", ""))
        return SkillResult(skill_name="google_calendar", success=True,
                           output=f"Event updated: '{updated.get('summary', event_id)}' → {start_out} [ID: {event_id}]")

    async def _delete_event(self, secrets: dict, event_id: str = "",
                            calendar_id: str = "primary", send_updates: str = "all",
                            **kwargs) -> SkillResult:
        if not event_id:
            return SkillResult(skill_name="google_calendar", success=False, output="",
                               error="'event_id' is required. Use list_events to find it.")
        token = await self._get_valid_token(secrets)
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.delete(
                f"{_CALENDAR_API}/calendars/{calendar_id}/events/{event_id}",
                headers={"Authorization": f"Bearer {token}"},
                params={"sendUpdates": send_updates},
            )
            if r.status_code == 404:
                return SkillResult(skill_name="google_calendar", success=False, output="",
                                   error=f"Event not found with ID '{event_id}'. Use list_events to get correct IDs.")
            if r.status_code == 410:
                return SkillResult(skill_name="google_calendar", success=False, output="",
                                   error=f"Event '{event_id}' was already deleted.")
            r.raise_for_status()
        return SkillResult(skill_name="google_calendar", success=True,
                           output=f"Event deleted successfully [ID: {event_id}]")

    async def _list_calendars(self, secrets: dict, **kwargs) -> SkillResult:
        token = await self._get_valid_token(secrets)
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(
                f"{_CALENDAR_API}/users/me/calendarList",
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()
            data = r.json()

        cals = data.get("items", [])
        if not cals:
            return SkillResult(skill_name="google_calendar", success=True,
                               output="No calendars found.")

        lines = [f"Calendars ({len(cals)}):"]
        for c in cals:
            primary = " [PRIMARY]" if c.get("primary") else ""
            access = c.get("accessRole", "")
            lines.append(f"• {c.get('summary', '?')}{primary} — ID: {c.get('id', '')} ({access})")
        return SkillResult(skill_name="google_calendar", success=True, output="\n".join(lines))

    async def _quick_add(self, secrets: dict, text: str = "",
                         calendar_id: str = "primary", **kwargs) -> SkillResult:
        """Create an event from a natural language string using Google's own parser."""
        if not text:
            return SkillResult(skill_name="google_calendar", success=False, output="",
                               error="'text' is required for quick_add e.g. 'Dentist tomorrow at 10am'")
        token = await self._get_valid_token(secrets)
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                f"{_CALENDAR_API}/calendars/{calendar_id}/events/quickAdd",
                headers={"Authorization": f"Bearer {token}"},
                params={"text": text},
            )
            r.raise_for_status()
            event = r.json()

        start_out = event.get("start", {}).get("dateTime", event.get("start", {}).get("date", ""))
        return SkillResult(skill_name="google_calendar", success=True,
                           output=f"Event created: '{event.get('summary', text)}' at {start_out} [ID: {event.get('id', '')}]")
