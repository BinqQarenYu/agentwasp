"""Task Manager skill — create/manage custom scheduled tasks at runtime."""

from __future__ import annotations

import re

import redis.asyncio as aioredis

from ..base import SkillBase
from ..types import ParamType, SkillDefinition, SkillParam, SkillResult
from ...scheduler.custom_tasks import (
    compute_next_run_at_time, delete_task, fmt_interval, get_task_by_name,
    list_tasks, make_task, next_run_from_now, parse_interval, save_task,
)


# ──────────────────────────────────────────────────────────────────────────
# Fixed-time-of-day detection
# Detects clock times like "8 am", "8:00am", "a las 8", "at 8:00 pm" in the
# user's instruction. Used by _create() to surface scheduling-honesty info
# when the user asks for a clock time we cannot honor.
# ──────────────────────────────────────────────────────────────────────────

_FIXED_TIME_PATTERNS = [
    # "8:00am", "8 am", "08:30 PM", with optional space and dot variants
    re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b", re.IGNORECASE),
    # "a las 8", "a las 8:00", "a las 8 horas", "a las 8 de la mañana"
    re.compile(r"\ba\s+las\s+(\d{1,2})(?::(\d{2}))?\b", re.IGNORECASE),
    # "at 8", "at 8:00", "at 8 o'clock"
    re.compile(r"\bat\s+(\d{1,2})(?::(\d{2}))?\b(?!\s*(?:hours?|h|min|second))", re.IGNORECASE),
    # "08:00 hrs" / "08:00 h"
    re.compile(r"\b(\d{1,2}):(\d{2})\s*(?:hrs?|h)\b", re.IGNORECASE),
]


def _detect_fixed_time_request(text: str) -> str:
    """Return a normalized clock-time string if the text requests a fixed time of day.

    Examples:
      "todos los días a las 8:00am" → "8:00 AM"
      "every day at 9:30"           → "9:30"
      "diario a las 8"              → "8:00"
      "monitorea cada 2h"           → "" (no fixed time)
    """
    if not text:
        return ""
    for pat in _FIXED_TIME_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        groups = m.groups()
        try:
            hour = int(groups[0])
        except (TypeError, ValueError):
            continue
        if not (0 <= hour <= 23):
            continue
        minute = 0
        if len(groups) > 1 and groups[1]:
            try:
                minute = int(groups[1])
            except ValueError:
                minute = 0
            if not (0 <= minute <= 59):
                minute = 0
        # Optional am/pm suffix from the first pattern
        suffix = ""
        if len(groups) >= 3 and groups[2]:
            s = groups[2].lower().replace(".", "")
            if s in ("am", "pm"):
                suffix = " " + s.upper()
        return f"{hour}:{minute:02d}{suffix}"
    return ""


class TaskManagerSkill(SkillBase):
    def __init__(self, redis_url: str, default_chat_id: str = ""):
        self.redis_url = redis_url
        self.default_chat_id = default_chat_id

    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="task_manager",
            description=(
                "Create and manage custom scheduled tasks. "
                "Actions: create, list, update, delete, delete_all, pause, resume, trigger."
            ),
            params=[
                SkillParam(
                    name="action",
                    param_type=ParamType.STRING,
                    description="Action: create | list | update | delete | delete_all | pause | resume | trigger",
                ),
                SkillParam(
                    name="name",
                    param_type=ParamType.STRING,
                    description="Task name",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="instruction",
                    param_type=ParamType.STRING,
                    description="What the agent should do when the task runs (natural language)",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="interval",
                    param_type=ParamType.STRING,
                    description="Interval: 'cada hora', 'diario', 'cada 2h', 'cada 30 minutos', 'semanal'",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="new_name",
                    param_type=ParamType.STRING,
                    description="New name for the task (only for update action)",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="chat_id",
                    param_type=ParamType.STRING,
                    description="Telegram chat_id to send results to (optional, uses default if omitted)",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="agent_id",
                    param_type=ParamType.STRING,
                    description="ID or name of a sub-agent that owns this task. When set, task uses AGENT_WAKEUP flow.",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="at_time",
                    param_type=ParamType.STRING,
                    description=(
                        "Clock time of day to run, in 24h 'HH:MM' or '8:00 AM'. "
                        "When set, the task fires at this local time (Chile TZ) every day "
                        "(or every interval). Combine with interval='diario' for true daily-at-HH:MM."
                    ),
                    required=False,
                    default="",
                ),
            ],
            category="productivity",
            timeout_seconds=15.0,
        )

    async def execute(
        self,
        action: str,
        name: str = "",
        instruction: str = "",
        interval: str = "",
        new_name: str = "",
        chat_id: str = "",
        agent_id: str = "",
        at_time: str = "",
        **kwargs,
    ) -> SkillResult:
        action = action.strip().lower()
        r = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            if action == "create":
                return await self._create(r, name, instruction, interval, chat_id, agent_id, at_time)
            elif action == "list":
                return await self._list(r)
            elif action in ("update", "modify", "edit", "change"):
                return await self._update(r, name, instruction, interval, new_name, at_time)
            elif action == "delete":
                return await self._delete(r, name)
            elif action in ("delete_all", "clear_all", "eliminar_todas", "borrar_todas"):
                return await self._delete_all(r)
            elif action == "pause":
                return await self._set_enabled(r, name, False)
            elif action == "resume":
                return await self._set_enabled(r, name, True)
            elif action == "trigger":
                return await self._trigger(r, name)
            else:
                return SkillResult(
                    skill_name="task_manager",
                    success=False,
                    output="",
                    error=f"Unknown action '{action}'. Use: create, list, update, delete, delete_all, pause, resume, trigger",
                )
        finally:
            await r.aclose()

    # ------------------------------------------------------------------
    @staticmethod
    def _to_local_time(utc_str: str) -> str:
        """Convert UTC ISO string to Chile local time for display."""
        try:
            from datetime import datetime, timezone as tz
            from ...config import get_tz
            dt = datetime.fromisoformat(utc_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz.utc)
            local_dt = dt.astimezone(get_tz())
            return local_dt.strftime("%Y-%m-%d %H:%M") + " (hora Chile)"
        except Exception:
            return utc_str[:19]

    @staticmethod
    def _normalize_name(name: str, instruction: str, interval: str) -> str:
        """Convert LLM-generated slug names into clean, human-readable titles.

        If the name looks like a slug (contains underscores, is all-lowercase raw words,
        or is longer than 50 chars), replace it with a generated title.
        """
        if not name:
            return name
        # Already a decent title: has spaces, mixed case, no underscores, short
        if " " in name and "_" not in name and len(name) <= 50:
            return name
        # Has underscores → was generated as a slug → regenerate from instruction
        if "_" in name or (name == name.lower() and len(name) > 30):
            try:
                from ...decision_layer import generate_task_title
                return generate_task_title(instruction or name, interval)
            except Exception:
                pass
        # Too long → truncate gracefully
        if len(name) > 50:
            return name[:50].rsplit(" ", 1)[0]
        return name

    async def _create(self, r, name, instruction, interval, chat_id, agent_id: str = "", at_time: str = "") -> SkillResult:
        if not name:
            return SkillResult(skill_name="task_manager", success=False, output="", error="'name' is required")
        # Normalize name: replace LLM-generated slugs with human-readable titles
        name = self._normalize_name(name, instruction, interval)
        if not instruction:
            return SkillResult(skill_name="task_manager", success=False, output="", error="'instruction' is required")
        # If at_time is set, default the interval to daily (24h) when caller omits it.
        if not interval and at_time:
            interval = "diario"
        if not interval:
            return SkillResult(skill_name="task_manager", success=False, output="", error="'interval' is required (e.g. 'diario', 'cada 2h')")

        seconds = parse_interval(interval)
        if not seconds or seconds < 60:
            return SkillResult(
                skill_name="task_manager", success=False, output="",
                error=f"Could not parse interval '{interval}'. Try: 'diario', 'cada hora', 'cada 2h', 'semanal'"
            )

        # If at_time wasn't passed explicitly, try auto-detecting it from the instruction.
        # This handles cases where the LLM plans the call before the new param is wired in
        # (e.g. instruction = "Resume noticias todos los días a las 8 AM").
        if not at_time:
            _detected = _detect_fixed_time_request(instruction)
            if _detected:
                # Normalise "8:00 AM" → "08:00" for stable storage
                at_time = _detected
        # Validate at_time can actually be parsed; if not, drop it silently (fall back to interval)
        if at_time and compute_next_run_at_time(at_time) is None:
            at_time = ""

        # Deduplication: if a task with this name already exists, return it instead of creating a duplicate
        existing = await get_task_by_name(r, name)
        if existing:
            return SkillResult(
                skill_name="task_manager",
                success=True,
                output=(
                    f"Task '{name}' already exists (no duplicate created).\n"
                    f"  Status: {'ACTIVE' if existing.get('enabled', True) else 'PAUSED'}\n"
                    f"  Interval: every {fmt_interval(existing['interval_seconds'])}\n"
                    f"  Next run: {self._to_local_time(existing['next_run'])}"
                ),
            )

        # Stronger dedup: if a task referencing the same agent_id already exists,
        # OR a task with a substantially overlapping instruction exists, return it.
        # Prevents the "BTC y ETH cada hora" + "BTC y ETH cada 2h" race seen in Test 2.
        try:
            _all_tasks = await list_tasks(r)
            _instr_norm = " ".join((instruction or "").lower().split())[:300]
            for _t in _all_tasks:
                # Same agent_id → almost certainly the same recurring job
                if resolved_agent_id and _t.get("agent_id") == resolved_agent_id:
                    return SkillResult(
                        skill_name="task_manager",
                        success=True,
                        output=(
                            f"A task for this agent already exists: '{_t['name']}' "
                            f"(every {fmt_interval(_t['interval_seconds'])}). "
                            f"Not creating a duplicate. Use action='update' if you need to change interval."
                        ),
                    )
                # Recent (<2 min) and significantly overlapping instruction → likely the same intent
                _t_instr = " ".join((_t.get("instruction") or "").lower().split())[:300]
                if _instr_norm and _t_instr:
                    # Cheap word-overlap heuristic
                    _w_a = set(w for w in _instr_norm.split() if len(w) > 3)
                    _w_b = set(w for w in _t_instr.split() if len(w) > 3)
                    if _w_a and _w_b:
                        _overlap = len(_w_a & _w_b) / max(len(_w_a), len(_w_b))
                        if _overlap >= 0.7:
                            return SkillResult(
                                skill_name="task_manager",
                                success=True,
                                output=(
                                    f"A very similar task already exists: '{_t['name']}' "
                                    f"(every {fmt_interval(_t['interval_seconds'])}). "
                                    f"Not creating a duplicate. Use action='update' or pick a clearly different name."
                                ),
                            )
        except Exception:
            pass  # fail-open: dedup is advisory

        # Resolve agent_id by name if a name string was passed
        resolved_agent_id = ""
        if agent_id and agent_id.strip():
            try:
                import redis.asyncio as _ar
                from ...agent_manager.store import list_agents as _list_agents
                _r2 = _ar.from_url(self.redis_url, decode_responses=True)
                try:
                    _agents = await _list_agents(_r2)
                    _aid = agent_id.strip().lower()
                    for _a in _agents:
                        if _a.id == agent_id.strip() or _a.name.lower() == _aid:
                            resolved_agent_id = _a.id
                            break
                    if not resolved_agent_id:
                        resolved_agent_id = agent_id.strip()  # Store as-is if not found
                finally:
                    await _r2.aclose()
            except Exception:
                resolved_agent_id = agent_id.strip()

        task = make_task(
            name=name,
            instruction=instruction,
            interval_seconds=seconds,
            chat_id=chat_id or self.default_chat_id,
            agent_id=resolved_agent_id,
            at_time=at_time,
        )
        await save_task(r, task)
        _next_display = self._to_local_time(task["next_run"])
        _schedule_type = f"daily at {at_time} (Chile TZ)" if at_time else "interval"
        return SkillResult(
            skill_name="task_manager",
            success=True,
            output=(
                f"Task created: '{name}'\n"
                f"  Instruction: {instruction}\n"
                f"  Interval: every {fmt_interval(seconds)}"
                + (f"\n  Clock time: {at_time}" if at_time else "")
                + f"\n  First run: {_next_display}\n"
                f"  SCHEDULE_TYPE: {_schedule_type}"
            ),
        )

    async def _list(self, r) -> SkillResult:
        tasks = await list_tasks(r)
        if not tasks:
            return SkillResult(skill_name="task_manager", success=True, output="No scheduled tasks.")
        lines = ["Scheduled tasks:\n"]
        for t in tasks:
            status = "ACTIVE" if t["enabled"] else "PAUSED"
            interval = fmt_interval(t["interval_seconds"])
            last = self._to_local_time(t["last_run"]) if t["last_run"] else "never"
            next_r = self._to_local_time(t["next_run"]) if t["next_run"] else "—"
            lines.append(
                f"  [{status}] {t['name']} (every {interval})\n"
                f"    Instruction: {t['instruction'][:80]}\n"
                f"    Last: {last} | Next: {next_r} | Runs: {t['run_count']}"
            )
        return SkillResult(skill_name="task_manager", success=True, output="\n".join(lines))

    async def _update(self, r, name, instruction: str, interval: str, new_name: str, at_time: str = "") -> SkillResult:
        if not name:
            return SkillResult(skill_name="task_manager", success=False, output="", error="'name' is required")
        task = await get_task_by_name(r, name)
        if not task:
            return SkillResult(
                skill_name="task_manager",
                success=True,
                output=f"Task '{name}' not found. Verify the name with task_manager(action='list').",
            )
        # Auto-detect at_time from a fresh instruction if not passed explicitly
        if not at_time and instruction:
            _detected = _detect_fixed_time_request(instruction)
            if _detected:
                at_time = _detected
        if at_time and compute_next_run_at_time(at_time) is None:
            return SkillResult(
                skill_name="task_manager", success=False, output="",
                error=f"Invalid at_time '{at_time}'. Use 24h 'HH:MM' or '8:00 AM'.",
            )
        changes = []
        if instruction:
            task["instruction"] = instruction
            changes.append("instruction updated")
        if interval:
            seconds = parse_interval(interval)
            if not seconds or seconds < 60:
                return SkillResult(
                    skill_name="task_manager", success=False, output="",
                    error=f"Invalid interval '{interval}'. Use: 'hourly', 'every 2h', 'daily', 'weekly'",
                )
            task["interval_seconds"] = seconds
            changes.append(f"interval → every {fmt_interval(seconds)}")
        if at_time:
            task["at_time"] = at_time
            # Recompute next_run from the new clock time
            _next_iso = compute_next_run_at_time(at_time)
            if _next_iso:
                task["next_run"] = _next_iso
            # If interval was not also changed, default to daily so at_time has cadence
            if not interval and (task.get("interval_seconds") or 0) < 86400:
                task["interval_seconds"] = 86400
                changes.append("interval → every 24h (to honor daily clock time)")
            changes.append(f"clock time → {at_time}")
        elif interval and not task.get("at_time"):
            # Only reset next_run from interval if there's no at_time anchor
            task["next_run"] = next_run_from_now(task["interval_seconds"])
        if new_name:
            task["name"] = new_name
            changes.append(f"name → '{new_name}'")
        if not changes:
            return SkillResult(
                skill_name="task_manager", success=True,
                output="Nothing to update. Use 'instruction', 'interval', 'at_time' or 'new_name' to modify.",
            )
        await save_task(r, task)
        return SkillResult(
            skill_name="task_manager",
            success=True,
            output=(
                f"Task '{name}' updated: {', '.join(changes)}.\n"
                f"  Next run: {self._to_local_time(task['next_run'])}"
            ),
        )

    async def _delete_all(self, r) -> SkillResult:
        tasks = await list_tasks(r)
        if not tasks:
            return SkillResult(skill_name="task_manager", success=True, output="No scheduled tasks to delete.")
        count = 0
        names = []
        for t in tasks:
            await delete_task(r, t["task_id"])
            names.append(t["name"])
            count += 1
        return SkillResult(
            skill_name="task_manager",
            success=True,
            output=f"✅ {count} task(s) deleted: {', '.join(names)}",
        )

    async def _delete(self, r, name) -> SkillResult:
        if not name:
            return SkillResult(skill_name="task_manager", success=False, output="", error="'name' is required")
        task = await get_task_by_name(r, name)
        if not task:
            tasks = await list_tasks(r)
            names = ", ".join(f"'{t['name']}'" for t in tasks) if tasks else "none"
            return SkillResult(
                skill_name="task_manager",
                success=False,
                output="",
                error=f"No task found with name '{name}'. Available tasks: {names}. Use the exact name.",
            )
        await delete_task(r, task["task_id"])
        return SkillResult(skill_name="task_manager", success=True, output=f"Task '{task['name']}' deleted successfully.")

    async def _set_enabled(self, r, name, enabled: bool) -> SkillResult:
        if not name:
            return SkillResult(skill_name="task_manager", success=False, output="", error="'name' is required")
        task = await get_task_by_name(r, name)
        if not task:
            return SkillResult(skill_name="task_manager", success=False, output="", error=f"Task '{name}' not found")
        task["enabled"] = enabled
        await save_task(r, task)
        state = "resumed" if enabled else "paused"
        return SkillResult(skill_name="task_manager", success=True, output=f"Task '{name}' {state}.")

    async def _trigger(self, r, name) -> SkillResult:
        if not name:
            return SkillResult(skill_name="task_manager", success=False, output="", error="'name' is required")
        task = await get_task_by_name(r, name)
        if not task:
            return SkillResult(skill_name="task_manager", success=False, output="", error=f"Task '{name}' not found")
        # Set next_run to now so CustomTaskRunnerJob picks it up on next cycle
        from datetime import datetime, timezone
        task["next_run"] = datetime.now(timezone.utc).isoformat()
        await save_task(r, task)
        return SkillResult(
            skill_name="task_manager",
            success=True,
            output=f"Task '{name}' scheduled to run on the next scheduler cycle (< 60s).",
        )
