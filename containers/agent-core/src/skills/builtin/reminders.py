from datetime import datetime, timezone

from ...config import get_tz, now_local
from ...db.session import async_session
from ...memory.manager import MemoryManager
from ...memory.types import MemoryQuery, MemoryType
from ..base import SkillBase
from ..types import ParamType, SkillDefinition, SkillParam, SkillResult


def _parse_due(due_str: str) -> str | None:
    """Parse a due string into ISO format in the configured timezone.

    Accepts:
      - ISO format: "2026-02-12T16:00:00"
      - Simple datetime: "2026-02-12 16:00"
      - Relative: "+5m", "+1h", "+2h30m"
      - Natural: "mañana 09:00", "hoy 15:00", "pasado mañana 10:00"
    Returns ISO string or None if unparseable.
    """
    import re
    from datetime import timedelta

    if not due_str:
        return None

    due_str = due_str.strip()
    tz = get_tz()

    # Relative time: +5m, +1h, +2h30m, +90s
    if due_str.startswith("+"):
        total_seconds = 0
        parts = re.findall(r"(\d+)([smhd])", due_str.lower())
        if not parts:
            return None
        for val, unit in parts:
            n = int(val)
            if unit == "s":
                total_seconds += n
            elif unit == "m":
                total_seconds += n * 60
            elif unit == "h":
                total_seconds += n * 3600
            elif unit == "d":
                total_seconds += n * 86400
        if total_seconds < 60:
            total_seconds = 60  # minimum 1 minute
        target = now_local() + timedelta(seconds=total_seconds)
        return target.isoformat()

    # Natural day references: "mañana 09:00", "hoy 15:30", "pasado mañana 10:00"
    day_offset = None
    time_part = None
    due_lower = due_str.lower()

    # Extract day offset
    if "pasado mañana" in due_lower or "pasado manana" in due_lower:
        day_offset = 2
    elif "mañana" in due_lower or "manana" in due_lower or "tomorrow" in due_lower:
        day_offset = 1
    elif "hoy" in due_lower or "today" in due_lower:
        day_offset = 0

    if day_offset is not None:
        # Extract time from the string: "09:00", "9am", "15:30", "9:00am", "9 am"
        time_m = re.search(r"(\d{1,2}):(\d{2})\s*([ap]m)?", due_lower)
        if not time_m:
            time_m = re.search(r"(\d{1,2})\s*([ap]m)", due_lower)
            if time_m:
                hour = int(time_m.group(1))
                ampm = time_m.group(2)
                if ampm == "pm" and hour < 12:
                    hour += 12
                elif ampm == "am" and hour == 12:
                    hour = 0
                time_part = (hour, 0)
        if time_m and time_part is None:
            hour = int(time_m.group(1))
            minute = int(time_m.group(2))
            ampm = time_m.group(3) if time_m.lastindex >= 3 else None
            if ampm == "pm" and hour < 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0
            time_part = (hour, minute)

        if time_part is None:
            # No time specified, default to 09:00
            time_part = (9, 0)

        local_now = now_local()
        target_date = local_now.date() + timedelta(days=day_offset)
        target = datetime(
            target_date.year, target_date.month, target_date.day,
            time_part[0], time_part[1], tzinfo=tz,
        )
        return target.isoformat()

    # Strip common natural-language prefixes before parsing
    # e.g. "cuando sean las 8:30", "a las 8:30 AM", "las 8:30"
    _clean = re.sub(
        r"^(?:cuando\s+sean?\s+las?\s+|a\s+las?\s+|las?\s+)",
        "", due_str, flags=re.IGNORECASE
    ).strip()

    # Handle 12-hour format with AM/PM: "8:30 AM", "8:30PM", "8 am", "8am"
    _ampm_m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)$", _clean, re.IGNORECASE)
    if _ampm_m:
        from datetime import timedelta
        hour   = int(_ampm_m.group(1))
        minute = int(_ampm_m.group(2) or 0)
        ampm   = _ampm_m.group(3).lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        local_now = now_local()
        parsed = datetime(local_now.year, local_now.month, local_now.day, hour, minute, tzinfo=tz)
        if parsed < local_now:
            parsed = parsed + timedelta(days=1)  # tomorrow same time
        return parsed.isoformat()

    # Try parsing as datetime string (also try cleaned version without prefix)
    for candidate in [due_str, _clean] if _clean != due_str else [due_str]:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                    "%d/%m/%Y %H:%M", "%H:%M"):
            try:
                parsed = datetime.strptime(candidate, fmt)
                if fmt == "%H:%M":
                    # Time only — assume today, if past assume tomorrow
                    from datetime import timedelta
                    local_now = now_local()
                    parsed = parsed.replace(year=local_now.year, month=local_now.month, day=local_now.day)
                    if parsed.replace(tzinfo=tz) < local_now:
                        parsed = parsed + timedelta(days=1)
                # Attach configured timezone
                parsed = parsed.replace(tzinfo=tz)
                return parsed.isoformat()
            except ValueError:
                continue

    return None  # Unparseable — caller will handle gracefully


class CreateReminderSkill(SkillBase):
    def __init__(self, memory: MemoryManager):
        self._memory = memory

    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="create_reminder",
            description="Create a timed reminder. Use due='+1m' for 1 minute, '+5m' for 5 minutes, '+1h' for 1 hour, or '2026-02-12 16:00' for exact time.",
            params=[
                SkillParam(name="text", param_type=ParamType.STRING, description="Reminder text"),
                SkillParam(name="due", param_type=ParamType.STRING, description="When: '+1m', '+5m', '+1h', '+2h30m', '16:00', '2026-02-12 16:00'", required=False, default=""),
            ],
            category="productivity",
        )

    async def execute(self, text: str, due: str = "", **kwargs) -> SkillResult:
        try:
            parsed_due = _parse_due(due)
            local_now = now_local()
            chat_id = kwargs.get("chat_id", "")

            async with async_session() as session:
                await self._memory.store_memory(
                    session,
                    memory_type=MemoryType.WORKING,
                    content={
                        "reminder_text": text,
                        "due": parsed_due or "",
                        "created_at": local_now.isoformat(),
                        "chat_id": chat_id,
                        "agent_id": kwargs.get("agent_id", ""),
                        "agent_objective": kwargs.get("agent_objective", ""),
                        "status": "active",
                    },
                    summary=f"Reminder: {text[:100]}",
                    tags=["reminder", "active"],
                )

            due_display = ""
            if parsed_due:
                try:
                    from datetime import datetime as dt
                    target = dt.fromisoformat(parsed_due)
                    due_display = f" (due: {target.strftime('%H:%M %d/%m/%Y')})"
                except Exception:
                    due_display = f" (due: {parsed_due})"

            return SkillResult(
                skill_name="create_reminder",
                success=True,
                output=f"Reminder created: {text}{due_display}",
            )
        except Exception as e:
            return SkillResult(skill_name="create_reminder", success=False, output="", error=str(e))


class ListRemindersSkill(SkillBase):
    def __init__(self, memory: MemoryManager):
        self._memory = memory

    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="list_reminders",
            description="List active reminders.",
            params=[],
            category="productivity",
        )

    async def execute(self, **kwargs) -> SkillResult:
        try:
            async with async_session() as session:
                entries = await self._memory.retrieve(
                    session,
                    MemoryQuery(memory_type=MemoryType.WORKING, tags=["reminder", "active"], limit=20),
                )
            if not entries:
                return SkillResult(skill_name="list_reminders", success=True, output="No active reminders.")

            lines = [f"Active reminders ({len(entries)}):"]
            for entry in entries:
                text = entry.content.get("reminder_text", "")
                due = entry.content.get("due", "")
                if due:
                    try:
                        target = datetime.fromisoformat(due)
                        due_str = f" (due: {target.strftime('%H:%M %d/%m/%Y')})"
                    except Exception:
                        due_str = f" (due: {due})"
                else:
                    due_str = ""
                lines.append(f"- {text}{due_str}")

            return SkillResult(skill_name="list_reminders", success=True, output="\n".join(lines))
        except Exception as e:
            return SkillResult(skill_name="list_reminders", success=False, output="", error=str(e))


class DeleteReminderSkill(SkillBase):
    """Delete a specific reminder or all reminders by keyword match."""

    def __init__(self, memory: MemoryManager):
        self._memory = memory

    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="delete_reminder",
            description=(
                "Delete a reminder by keyword or index. "
                "Use keyword to match by text, or 'all' to delete all active reminders."
            ),
            params=[
                SkillParam(
                    name="keyword",
                    param_type=ParamType.STRING,
                    description="Text to match against reminder content, or 'all' to delete all",
                ),
            ],
            category="productivity",
        )

    async def execute(self, keyword: str = "", **kwargs) -> SkillResult:
        keyword = (keyword or "").strip()
        if not keyword:
            return SkillResult(
                skill_name="delete_reminder", success=False, output="",
                error="'keyword' is required. Use 'all' to delete all reminders.",
            )
        try:
            async with async_session() as session:
                entries = await self._memory.retrieve(
                    session,
                    MemoryQuery(memory_type=MemoryType.WORKING, tags=["reminder", "active"], limit=50),
                )

            if not entries:
                return SkillResult(
                    skill_name="delete_reminder", success=True,
                    output="No active reminders to delete.",
                )

            deleted_names: list[str] = []
            kw_lower = keyword.lower()

            for entry in entries:
                text = entry.content.get("reminder_text", "")
                if kw_lower == "all" or kw_lower in text.lower():
                    async with async_session() as session:
                        await self._memory.delete(session, MemoryType.WORKING, entry.id)
                    deleted_names.append(text[:60] or entry.id[:8])

            if not deleted_names:
                return SkillResult(
                    skill_name="delete_reminder", success=True,
                    output=f"No reminders matched '{keyword}'.",
                )

            count = len(deleted_names)
            names_str = ", ".join(f'"{n}"' for n in deleted_names[:5])
            return SkillResult(
                skill_name="delete_reminder", success=True,
                output=f"{count} reminder(s) deleted: {names_str}",
            )
        except Exception as e:
            return SkillResult(
                skill_name="delete_reminder", success=False, output="", error=str(e),
            )
