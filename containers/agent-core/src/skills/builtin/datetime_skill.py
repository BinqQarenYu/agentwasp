import zoneinfo
from datetime import datetime, timezone

from ..base import SkillBase
from ..types import ParamType, SkillDefinition, SkillParam, SkillResult

# Common city/country -> IANA timezone mapping for fuzzy resolution
CITY_TIMEZONE_MAP = {
    "santiago": "America/Santiago",
    "chile": "America/Santiago",
    "buenos aires": "America/Argentina/Buenos_Aires",
    "argentina": "America/Argentina/Buenos_Aires",
    "new york": "America/New_York",
    "los angeles": "America/Los_Angeles",
    "chicago": "America/Chicago",
    "london": "Europe/London",
    "paris": "Europe/Paris",
    "madrid": "Europe/Madrid",
    "berlin": "Europe/Berlin",
    "rome": "Europe/Rome",
    "tokyo": "Asia/Tokyo",
    "beijing": "Asia/Shanghai",
    "shanghai": "Asia/Shanghai",
    "sydney": "Australia/Sydney",
    "moscow": "Europe/Moscow",
    "dubai": "Asia/Dubai",
    "mumbai": "Asia/Kolkata",
    "india": "Asia/Kolkata",
    "mexico": "America/Mexico_City",
    "bogota": "America/Bogota",
    "colombia": "America/Bogota",
    "lima": "America/Lima",
    "peru": "America/Lima",
    "sao paulo": "America/Sao_Paulo",
    "brazil": "America/Sao_Paulo",
    "caracas": "America/Caracas",
    "venezuela": "America/Caracas",
}


def _resolve_timezone(tz: str) -> zoneinfo.ZoneInfo | timezone:
    """Resolve a timezone string, handling common variations and city names."""
    tz = tz.strip()
    if not tz or tz.upper() == "UTC":
        return timezone.utc

    # Try direct IANA lookup first
    try:
        return zoneinfo.ZoneInfo(tz)
    except (KeyError, zoneinfo.ZoneInfoNotFoundError):
        pass

    # Normalize: remove extra spaces, fix slashes
    normalized = "/".join(part.strip() for part in tz.split("/"))
    try:
        return zoneinfo.ZoneInfo(normalized)
    except (KeyError, zoneinfo.ZoneInfoNotFoundError):
        pass

    # Try city/country fuzzy lookup
    key = tz.lower().replace("/", " ").replace("_", " ").strip()
    if key in CITY_TIMEZONE_MAP:
        return zoneinfo.ZoneInfo(CITY_TIMEZONE_MAP[key])

    # Try partial match in city map
    for city, iana in CITY_TIMEZONE_MAP.items():
        if city in key or key in city:
            return zoneinfo.ZoneInfo(iana)

    # Try searching available timezones
    tz_lower = tz.lower().replace(" ", "_")
    for available_tz in sorted(zoneinfo.available_timezones()):
        if tz_lower in available_tz.lower():
            return zoneinfo.ZoneInfo(available_tz)

    raise ValueError(f"Unknown timezone: {tz}. Use IANA format like 'America/Santiago' or a city name like 'Santiago'.")


class GetDatetimeSkill(SkillBase):
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="get_datetime",
            description="Get the current date and time.",
            params=[
                SkillParam(
                    name="tz",
                    param_type=ParamType.STRING,
                    description="Timezone: IANA format (e.g. 'America/Santiago') or city name (e.g. 'Santiago'). Default: UTC",
                    required=False,
                    default="UTC",
                ),
            ],
            category="utility",
            timeout_seconds=5.0,
        )

    async def execute(self, tz: str = "UTC", **kwargs) -> SkillResult:
        try:
            tzinfo = _resolve_timezone(tz)
            now = datetime.now(tzinfo)

            # Day and month names in Spanish for pre-formatted output
            days_es = {
                "Monday": "lunes", "Tuesday": "martes", "Wednesday": "miércoles",
                "Thursday": "jueves", "Friday": "viernes", "Saturday": "sábado",
                "Sunday": "domingo",
            }
            months_es = {
                1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
                5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
                9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre",
            }
            day_name = days_es.get(now.strftime("%A"), now.strftime("%A"))
            month_name = months_es.get(now.month, now.strftime("%B"))

            # Pre-formatted sentence the LLM can just repeat verbatim
            hour = now.hour
            minute = now.minute
            output = (
                f"Son las {hour}:{minute:02d} del {day_name} "
                f"{now.day} de {month_name} de {now.year}."
            )
            return SkillResult(skill_name="get_datetime", success=True, output=output)
        except Exception as e:
            return SkillResult(skill_name="get_datetime", success=False, output="", error=str(e))
