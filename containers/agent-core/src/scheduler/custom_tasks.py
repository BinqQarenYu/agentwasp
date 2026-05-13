"""Custom scheduled tasks — Redis-backed storage and helpers."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

REDIS_KEY = "custom_tasks"


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def next_run_from_now(interval_seconds: int) -> str:
    """Return ISO UTC string for now + interval_seconds."""
    return (_now_utc() + timedelta(seconds=interval_seconds)).isoformat()


def compute_next_run_at_time(at_time: str) -> str | None:
    """Compute the next ISO UTC datetime where local clock = at_time (HH:MM).

    Uses the system's configured local timezone (via config.get_tz). If now is
    before today's at_time, returns today at_time; otherwise returns tomorrow
    at_time. Returns None if at_time cannot be parsed.

    Examples (assume Chile TZ, now = 09:00 local):
      "08:00" → tomorrow 08:00 local
      "10:30" → today 10:30 local
    """
    if not at_time:
        return None
    s = str(at_time).strip().lower().replace(".", "")
    am_pm = ""
    if s.endswith("am") or s.endswith("pm"):
        am_pm = s[-2:]
        s = s[:-2].strip()
    parts = s.split(":")
    try:
        hour = int(parts[0])
    except (ValueError, IndexError):
        return None
    minute = 0
    if len(parts) >= 2:
        try:
            minute = int(parts[1])
        except ValueError:
            minute = 0
    if am_pm == "pm" and hour < 12:
        hour += 12
    if am_pm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    try:
        from ..config import get_tz  # local TZ (Chile by default)
        tz = get_tz()
    except Exception:
        tz = timezone.utc
    now_local = datetime.now(tz)
    target_local = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    # If the target time has already passed today, schedule it for tomorrow
    if target_local <= now_local:
        target_local = target_local + timedelta(days=1)
    return target_local.astimezone(timezone.utc).isoformat()


def parse_interval(text: str) -> int | None:
    """Parse human interval text into seconds.

    Examples:
        "cada hora"      → 3600
        "cada 2h"        → 7200
        "cada 30 minutos"→ 1800
        "diario"         → 86400
        "semanal"        → 604800
        "cada 3 días"    → 259200
        3600             → 3600  (int passthrough)
    """
    if isinstance(text, int):
        return text

    t = str(text).strip().lower()

    # "cada N <unit>" — try the numeric form FIRST. The previous order
    # checked named-shortcut substrings first, which mis-routed "6 horas"
    # to "hora" → 3600 (one hour) instead of 21600 (six hours).
    m = re.search(
        r"(?:cada\s+|every\s+)?(\d+(?:\.\d+)?)\s*"
        r"(min(?:uto)?s?|h(?:ora)?s?|hour(?:s)?|d[ií]as?|days?|semanas?|weeks?|seg(?:undo)?s?)",
        t,
    )
    if m:
        n = float(m.group(1))
        unit = m.group(2).lower()
        if unit.startswith("seg"):
            return int(n)
        if unit.startswith("min"):
            return int(n * 60)
        if unit.startswith("h") or unit.startswith("hour"):
            return int(n * 3600)
        if unit.startswith("d"):
            return int(n * 86400)
        if unit.startswith("sem") or unit.startswith("week"):
            return int(n * 604800)

    # Named shortcuts (no leading number) — fallback after numeric form.
    named = {
        "hourly": 3600, "hora": 3600,
        "6h": 21600,
        "12h": 43200,
        "diariamente": 86400,
        "diario": 86400, "daily": 86400, "cada día": 86400, "cada dia": 86400,
        "semanal": 604800, "weekly": 604800, "cada semana": 604800,
        "mensual": 2592000, "monthly": 2592000,
    }
    for key, val in named.items():
        if key in t:
            return val

    # Plain integer string
    if t.isdigit():
        return int(t)

    return None


def make_task(
    name: str,
    instruction: str,
    interval_seconds: int,
    chat_id: str = "",
    task_id: str | None = None,
    agent_id: str = "",
    at_time: str = "",
    next_run_override: str = "",
) -> dict:
    """Build a task record.

    Scheduling fields:
      interval_seconds   — gap between consecutive runs after the first
      at_time            — clock time "HH:MM" (24h) or "8:30 AM"; if set,
                           first_run is computed to next local at_time
      next_run_override  — explicit ISO UTC for the first run; wins over both
                           interval/at_time when present
    """
    now = _now_utc().isoformat()
    if next_run_override:
        first_run = next_run_override
    elif at_time:
        first_run = compute_next_run_at_time(at_time) or next_run_from_now(interval_seconds)
    else:
        first_run = next_run_from_now(interval_seconds)
    return {
        "task_id": task_id or str(uuid4()),
        "name": name,
        "instruction": instruction,
        "interval_seconds": interval_seconds,
        "next_run": first_run,
        "at_time": at_time,           # persisted so re-schedules after each run hit the same clock
        "last_run": None,
        "last_result": "",
        "last_success": None,
        "run_count": 0,
        "failure_count": 0,
        "enabled": True,
        "chat_id": chat_id,
        "created_at": now,
        "agent_id": agent_id,  # Optional: link to a sub-agent (enables AGENT_WAKEUP flow)
    }


# ---------------------------------------------------------------------------
# Redis HASH storage
# ---------------------------------------------------------------------------

async def list_tasks(r) -> list[dict]:
    """Return all tasks sorted by created_at."""
    raw = await r.hgetall(REDIS_KEY)
    tasks = []
    for v in raw.values():
        try:
            tasks.append(json.loads(v))
        except Exception:
            pass
    tasks.sort(key=lambda t: t.get("created_at", ""))
    return tasks


async def get_task(r, task_id: str) -> dict | None:
    raw = await r.hget(REDIS_KEY, task_id)
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return None


async def get_task_by_name(r, name: str) -> dict | None:
    """Find task by exact name first, then by partial-and-unambiguous match.

    Match policy:
      1. Exact (case-insensitive) match → return.
      2. Partial substring match → return ONLY if exactly one task matches.
         If multiple tasks contain or are contained in the needle, return
         None — caller must ask the user which one.
      3. Word-overlap fallback: tokens shared between needle and task name.
         Returns the task with the highest overlap, if it has at least 2
         distinct shared tokens AND no other task ties for that overlap.

    Without these constraints the previous loose match ("needle in name OR
    name in needle") routinely picked the WRONG task when the user named
    one task and another had a partial substring match (e.g. user said
    "Monitoreo de clima cada 3h" but the LLM truncated `name` to "cada
    hora", which matched "Informe cada hora" by partial).
    """
    needle = (name or "").strip().lower()
    if not needle:
        return None
    tasks = await list_tasks(r)
    # 1. Exact match
    for task in tasks:
        if task["name"].lower() == needle:
            return task
    # 2. Partial substring — only if uniquely matching
    partial = [t for t in tasks if (needle in t["name"].lower() or t["name"].lower() in needle)]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        # Ambiguous — caller should disambiguate with the user.
        return None
    # 3. Word-overlap with strict threshold
    needle_words = {w for w in re.split(r"\W+", needle) if len(w) >= 3}
    if not needle_words:
        return None
    best_overlap, best_task, ties = 0, None, 0
    for t in tasks:
        t_words = {w for w in re.split(r"\W+", t["name"].lower()) if len(w) >= 3}
        overlap = len(needle_words & t_words)
        if overlap > best_overlap:
            best_overlap, best_task, ties = overlap, t, 0
        elif overlap == best_overlap and overlap > 0:
            ties += 1
    if best_task is not None and best_overlap >= 2 and ties == 0:
        return best_task
    return None


async def save_task(r, task: dict) -> None:
    await r.hset(REDIS_KEY, task["task_id"], json.dumps(task, default=str))


async def delete_task(r, task_id: str) -> bool:
    result = await r.hdel(REDIS_KEY, task_id)
    return result > 0


def fmt_interval(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"
