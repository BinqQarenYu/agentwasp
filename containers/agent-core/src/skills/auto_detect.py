"""Auto-detect which skills should be pre-executed based on user message.

Small models (1.5B-3B) can't reliably emit <skill> tags, so we detect
common patterns in the user's message and pre-execute the relevant skills.
The results are injected into the LLM context so it only needs to format them.
"""

import re
import urllib.parse

from .types import SkillCall


# Site-name → URL mapping intentionally removed: hardcoding aggregators or
# news outlets biases the agent toward a specific source. When the user
# names a site without a URL, fall through to web_search at runtime so the
# resolution is neutral and works for ANY site, not just a curated list.


def _normalize_browser_url(url: str) -> str:
    """Normalize a browser URL: ensure scheme and www prefix for known bare domains.

    Does NOT silently redirect to alternative sites — respects user's explicit URL choice.
    """
    if not url.startswith("http"):
        url = "https://" + url

    # Add www. prefix for known domains that require it (bare domain = connection reset)
    _NEEDS_WWW = {"lasegunda.com", "elmercurio.com", "latercera.com"}
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.netloc in _NEEDS_WWW:
            url = url.replace(f"://{parsed.netloc}", f"://www.{parsed.netloc}", 1)
    except Exception:
        pass

    return url

# Patterns for datetime detection
_DATETIME_PATTERNS = [
    re.compile(r"\b(?:que|qué|cual|cuál)\s+(?:es\s+)?(?:la\s+)?hora\b", re.IGNORECASE),
    re.compile(r"\bhora\s+(?:en|de|actual)\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+time\b", re.IGNORECASE),
    re.compile(r"\bcurrent\s+time\b", re.IGNORECASE),
    re.compile(r"\bqué\s+fecha\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+(?:is\s+the\s+)?date\b", re.IGNORECASE),
]

# Patterns for weather detection
_WEATHER_PATTERNS = [
    re.compile(r"\bclima\s+(?:en|de)\b", re.IGNORECASE),
    re.compile(r"\bclima\b.*(?:en|de)\b", re.IGNORECASE),
    re.compile(r"\b(?:como|cómo)\s+(?:está|esta)\s+el\s+(?:clima|tiempo)\b", re.IGNORECASE),
    re.compile(r"\btemperatura\s+(?:en|de)\b", re.IGNORECASE),
    re.compile(r"\bweather\s+(?:in|at|for)\b", re.IGNORECASE),
    re.compile(r"\bel\s+clima\s+de\b", re.IGNORECASE),
    re.compile(r"\bel\s+tiempo\s+en\b", re.IGNORECASE),
]

# Patterns for web search detection
_SEARCH_PATTERNS = [
    re.compile(r"\bbusca(?:r|me)?\s+(?:en\s+(?:internet|la\s+web|google)\s+)?(.+)", re.IGNORECASE),
    re.compile(r"\binvestiga(?:r)?\s+(?:sobre\s+)?(.+)", re.IGNORECASE),
    re.compile(r"\bsearch\s+(?:for\s+|the\s+web\s+for\s+)?(.+)", re.IGNORECASE),
    re.compile(r"\bgoogle(?:a(?:r|ndo|me)?|ear?)\s+(.+)", re.IGNORECASE),
    re.compile(r"\blook\s+up\s+(.+)", re.IGNORECASE),
    re.compile(r"\bfind\s+(?:information\s+(?:about|on)\s+)?(.+)", re.IGNORECASE),
    re.compile(r"\b(?:que|qué)\s+(?:es|son|fue|significa)\s+(.+)", re.IGNORECASE),
    re.compile(r"\b(?:quien|quién)\s+(?:es|fue|era)\s+(.+)", re.IGNORECASE),
    re.compile(r"\bwhat\s+(?:is|are|was)\s+(.+)", re.IGNORECASE),
    re.compile(r"\bwho\s+(?:is|was)\s+(.+)", re.IGNORECASE),
    re.compile(r"\bdime\s+(?:sobre|acerca\s+de|que\s+es|qué\s+es)\s+(.+)", re.IGNORECASE),
    re.compile(r"\bcuenta(?:me)?\s+(?:sobre|de|acerca)\s+(.+)", re.IGNORECASE),
    re.compile(r"\binfo(?:rmación)?\s+(?:sobre|de|acerca)\s+(.+)", re.IGNORECASE),
    re.compile(r"\bnoticias\s+(?:sobre|de)\s+(.+)", re.IGNORECASE),
    re.compile(r"\bnews\s+(?:about|on)\s+(.+)", re.IGNORECASE),
]

# URL pattern for fetch_url auto-detection
_URL_PATTERN = re.compile(r"(https?://[^\s]+)", re.IGNORECASE)

# YouTube URL pattern (watch or short link)
_YOUTUBE_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?(?:youtube\.com/watch\?(?:[^\s]*&)?v=|youtu\.be/)([a-zA-Z0-9_-]{11})",
    re.IGNORECASE,
)

# Bare domain pattern (sweetprompt.com, example.org, etc.)
_DOMAIN_PATTERN = re.compile(
    r"\b([a-zA-Z0-9][-a-zA-Z0-9]*\.(?:com|org|net|io|dev|ai|app|co|cl|ar|mx|es|uk|de|fr|br|xyz|me|info|tech|pro|site|online|store|shop))\b",
    re.IGNORECASE,
)

# Content/news keywords for domain detection (compiled at module level, not in hot-path)
_CONTENT_WORDS = re.compile(
    r"\b(?:noticias?|articulos?|artículos?|news|articles?|"
    r"contenido|content|titulares?|headlines?|portada|front\s*page)\b",
    re.IGNORECASE,
)

# Price/buy comparison patterns (compiled at module level, not in hot-path)
_PRICE_PATTERNS = [
    re.compile(r"\b(?:cuánto|cuanto)\s+(?:cuesta|vale|sale|cost(?:a|an)?)\b", re.IGNORECASE),
    re.compile(r"\bprecio\s+(?:de(?:l?)?|del)\b", re.IGNORECASE),
    re.compile(r"\bdonde\s+(?:puedo\s+)?comprar\b", re.IGNORECASE),
    re.compile(r"\bm[aá]s\s+barato\b", re.IGNORECASE),
    re.compile(r"\bhow\s+much\s+(?:is|does|cost)\b", re.IGNORECASE),
    re.compile(r"\bbest\s+price\b", re.IGNORECASE),
    re.compile(r"\bcheapest\b", re.IGNORECASE),
    re.compile(r"\bdame\s+(?:los\s+)?(?:links?|precios|urls?)\b", re.IGNORECASE),
    re.compile(r"\b(?:links?|urls?)\s+con\s+(?:los\s+)?precios\b", re.IGNORECASE),
    re.compile(r"\bver\s+precios\b", re.IGNORECASE),
]

# Patterns that request reading/visiting a URL (when URL is present)
_FETCH_PATTERNS = [
    re.compile(r"\b(?:lee|leer|abre|abrir|revisa|revisar|mira|mirar|visita|entra)\b", re.IGNORECASE),
    re.compile(r"\b(?:read|open|visit|check|fetch|go\s+to|look\s+at|see)\b", re.IGNORECASE),
    re.compile(r"\b(?:qué\s+(?:hay|dice|tiene)|what(?:'s|\s+is)\s+(?:on|in|at))\b", re.IGNORECASE),
    re.compile(r"\b(?:resumen|resume|resúmeme|summarize|summary)\b", re.IGNORECASE),
    re.compile(r"\b(?:contenido|content)\b", re.IGNORECASE),
]

# Queries too short/generic to search (avoid false positives)
_SEARCH_IGNORE = {
    "eso", "esto", "esa", "ese", "algo", "nada", "todo",
    "that", "this", "it", "something", "nothing",
}

# Timezone mapping for common location mentions
_LOCATION_TIMEZONES = {
    "santiago": "America/Santiago",
    "chile": "America/Santiago",
    "buenos aires": "America/Argentina/Buenos_Aires",
    "argentina": "America/Argentina/Buenos_Aires",
    "bogota": "America/Bogota",
    "bogotá": "America/Bogota",
    "colombia": "America/Bogota",
    "lima": "America/Lima",
    "peru": "America/Lima",
    "perú": "America/Lima",
    "mexico": "America/Mexico_City",
    "méxico": "America/Mexico_City",
    "caracas": "America/Caracas",
    "venezuela": "America/Caracas",
    "new york": "America/New_York",
    "nueva york": "America/New_York",
    "los angeles": "America/Los_Angeles",
    "los ángeles": "America/Los_Angeles",
    "chicago": "America/Chicago",
    "london": "Europe/London",
    "londres": "Europe/London",
    "paris": "Europe/Paris",
    "parís": "Europe/Paris",
    "madrid": "Europe/Madrid",
    "españa": "Europe/Madrid",
    "spain": "Europe/Madrid",
    "berlin": "Europe/Berlin",
    "alemania": "Europe/Berlin",
    "rome": "Europe/Rome",
    "roma": "Europe/Rome",
    "tokyo": "Asia/Tokyo",
    "tokio": "Asia/Tokyo",
    "japan": "Asia/Tokyo",
    "japón": "Asia/Tokyo",
    "beijing": "Asia/Shanghai",
    "shanghai": "Asia/Shanghai",
    "china": "Asia/Shanghai",
    "sydney": "Australia/Sydney",
    "moscow": "Europe/Moscow",
    "moscú": "Europe/Moscow",
    "dubai": "Asia/Dubai",
    "mumbai": "Asia/Kolkata",
    "india": "Asia/Kolkata",
    "sao paulo": "America/Sao_Paulo",
    "são paulo": "America/Sao_Paulo",
    "brazil": "America/Sao_Paulo",
    "brasil": "America/Sao_Paulo",
}


def _extract_location(text: str) -> str:
    """Extract a location name from the user's text."""
    text_lower = text.lower()

    # Remove common prefixes
    for prefix in ["la hora de ", "la hora en ", "hora de ", "hora en ",
                   "el clima de ", "el clima en ", "clima de ", "clima en ",
                   "el tiempo en ", "el tiempo de ",
                   "weather in ", "weather for ", "time in ",
                   "temperatura de ", "temperatura en "]:
        if prefix in text_lower:
            idx = text_lower.index(prefix) + len(prefix)
            return text[idx:].strip().rstrip("?!.")

    # Try to find any known location
    for loc in sorted(_LOCATION_TIMEZONES.keys(), key=len, reverse=True):
        if loc in text_lower:
            return loc

    return ""


def _resolve_timezone(location: str) -> str:
    """Resolve a location to an IANA timezone."""
    loc_lower = location.lower()

    # Direct match
    for key, tz in _LOCATION_TIMEZONES.items():
        if key in loc_lower:
            return tz

    # Default to UTC if no location found
    return "UTC"


def _extract_urls(text: str) -> list[str]:
    """Extract all URLs from text."""
    return _URL_PATTERN.findall(text)


def _extract_search_query(text: str) -> str:
    """Extract the search query from a user message."""
    for pattern in _SEARCH_PATTERNS:
        m = pattern.search(text)
        if m and m.group(1):
            query = m.group(1).strip().rstrip("?!.")
            if query.lower() not in _SEARCH_IGNORE and len(query) > 2:
                return query
    return ""


# Reminder trigger verbs
_REM_VERBS = (
    r"(?:recu[eé]rd(?:a|e)me|avisame|av[ií]same|recordar(?:me)?|remind\s*me|"
    r"crea(?:me)?\s+(?:un\s+)?recordatorio|pon(?:me|er)?\s+(?:un\s+)?recordatorio|"
    r"set\s+(?:a\s+)?reminder|create\s+(?:a\s+)?reminder|"
    r"haz(?:me)?\s+(?:un\s+)?recordatorio|programa(?:me)?\s+(?:un\s+)?recordatorio)"
)

# Patterns for reminder detection
_REMINDER_PATTERNS = [
    # "recuerdame/avísame en X minutos que/para ..." (with reminder text)
    re.compile(
        _REM_VERBS + r"\s+(?:en\s+|in\s+)?(\d+)\s*"
        r"(minuto|minute|min|m|hora|hour|h|segundo|second|seg|s|día|dia|day|d)s?\b"
        r"(?:\s+(?:que|to|para)\s+|\s+)(.+)",
        re.IGNORECASE,
    ),
    # "en X minutos recuerdame/creame recordatorio que ..."
    re.compile(
        r"\ben\s+(\d+)\s*(minuto|minute|min|m|hora|hour|h|segundo|second|seg|s|día|dia|day|d)s?\s+"
        + _REM_VERBS + r"\s+(?:que\s+|to\s+|para\s+)?(.+)",
        re.IGNORECASE,
    ),
    # "recordatorio en X minutos: ..."
    re.compile(
        r"\brecordatorio\s+(?:en\s+)?(\d+)\s*"
        r"(minuto|minute|min|m|hora|hour|h|segundo|second|seg|s|día|dia|day|d)s?\b"
        r"(?:\s*[:]\s*|\s+(?:que|para|de)\s+|\s+)(.+)",
        re.IGNORECASE,
    ),
    # "avísame en 5 minutos" / "recuérdame en 1 hora" (NO reminder text — just time)
    re.compile(
        _REM_VERBS + r"\s+(?:en\s+|in\s+)?(\d+)\s*"
        r"(minuto|minute|min|m|hora|hour|h|segundo|second|seg|s|día|dia|day|d)s?\s*$",
        re.IGNORECASE,
    ),
]

_TIME_UNIT_MAP = {
    "minuto": "m", "min": "m", "m": "m", "minute": "m", "minutes": "m",
    "hora": "h", "hour": "h", "h": "h", "hours": "h",
    "segundo": "s", "seg": "s", "s": "s", "second": "s", "seconds": "s",
    "día": "d", "dia": "d", "day": "d", "d": "d", "days": "d",
}

# Number words -> digits (for "un minuto", "cinco minutos", etc.)
_NUMBER_WORDS = {
    "un": "1", "uno": "1", "una": "1", "one": "1",
    "dos": "2", "two": "2",
    "tres": "3", "three": "3",
    "cuatro": "4", "four": "4",
    "cinco": "5", "five": "5",
    "seis": "6", "six": "6",
    "siete": "7", "seven": "7",
    "ocho": "8", "eight": "8",
    "nueve": "9", "nine": "9",
    "diez": "10", "ten": "10",
    "quince": "15", "fifteen": "15",
    "veinte": "20", "twenty": "20",
    "treinta": "30", "thirty": "30",
    "media": "30",  # "media hora" = 30 minutes
}

_NUMBER_WORD_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in sorted(_NUMBER_WORDS.keys(), key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def _normalize_number_words(text: str) -> str:
    """Replace number words with digits for regex matching."""
    # Special: "media hora" -> "30 minutos"
    result = re.sub(r"\bmedia\s+hora\b", "30 minutos", text, flags=re.IGNORECASE)
    def _replace(m):
        return _NUMBER_WORDS.get(m.group(1).lower(), m.group(1))
    return _NUMBER_WORD_PATTERN.sub(_replace, result)


# Domain TLD pattern shared across monitor detection
_TLD_PATTERN = r"(?:com|org|net|io|dev|ai|app|co|cl|ar|mx|es|uk|de|fr|br|xyz|me|info|tech|pro|site|online|store|shop)"
_URL_OR_DOMAIN = (
    r"(https?://[^\s]+|[a-zA-Z0-9][-a-zA-Z0-9]*\." + _TLD_PATTERN + r"(?:/[^\s]*)?)"
)

# Monitor patterns (order matters: keyword-specific first, then general)
_MONITOR_PATTERNS = [
    # Pattern 0: "vigila URL por KEYWORD" / "watch URL for KEYWORD" / "monitorea URL buscando X"
    re.compile(
        r"\b(?:monitor(?:ea|ear)?|vigila(?:r)?|watch|observa(?:r)?)\s+"
        + _URL_OR_DOMAIN
        + r"\s+(?:por|buscando|si\s+aparece|cuando\s+aparezca|for|if)\s+"
        r"[\"']?(.+?)[\"']?\s*$",
        re.IGNORECASE,
    ),
    # Pattern 1: "avisame si cambia URL"
    re.compile(
        r"\b(?:avis(?:a|e)me|notif(?:y|ica|ícame)|alert(?:a|ame)?)\s+"
        r"(?:si|cuando|if|when)\s+"
        r"(?:cambia|hay\s+cambios?\s+en|(?:something\s+)?chang(?:es|ed?)\s+(?:on|in|at))\s+"
        + _URL_OR_DOMAIN,
        re.IGNORECASE,
    ),
    # Pattern 2: "monitorea/vigila/watch URL cada X minutos/horas" (general, last)
    re.compile(
        r"\b(?:monitor(?:ea|ear)?|vigila(?:r)?|watch|observa(?:r)?)\s+"
        + _URL_OR_DOMAIN
        + r"(?:\s+(?:cada|every)\s+(\d+)\s*(minuto|minute|min|hora|hour|h|día|dia|day)s?)?"
        + r"(?:\s+.*)?",
        re.IGNORECASE,
    ),
]

_INTERVAL_UNIT_MAP = {
    "minuto": 1, "minute": 1, "min": 1,
    "hora": 60, "hour": 60, "h": 60,
    "día": 1440, "dia": 1440, "day": 1440,
}


# Patterns for monitor listing/querying (bypass LLM completely)
_MONITOR_LIST_PATTERNS = [
    re.compile(r"\b(?:tienes?|hay|existen?)\s+(?:algún|algun|alguno|algunos)?\s*monitor(?:eo|es)?s?\b", re.IGNORECASE),
    re.compile(r"\b(?:que|qué|cuáles?|cuales?)\s+monitor(?:eo|es)?s?\s+(?:hay|tienes?|tengo|están?|estan?)\b", re.IGNORECASE),
    re.compile(r"\bmis\s+monitor(?:eo|es)?s?\b", re.IGNORECASE),
    re.compile(r"\blist(?:a|ar)?\s+monitor(?:eo|es)?s?\b", re.IGNORECASE),
    re.compile(r"\bmonitor(?:eo|es)?s?\s+activos?\b", re.IGNORECASE),
    re.compile(r"\bshow\s+monitors?\b", re.IGNORECASE),
    re.compile(r"\bque\s+(?:estas?|estás?)\s+monitor(?:eando|izando)\b", re.IGNORECASE),
    re.compile(r"\bque\s+(?:sitios?|paginas?|páginas?|webs?)\s+(?:estas?|estás?)\s+(?:vigilando|monitoreando|observando)\b", re.IGNORECASE),
]

# Patterns for reminder listing (bypass LLM completely)
_REMINDER_LIST_PATTERNS = [
    re.compile(r"\b(?:tienes?|hay|existen?)\s+(?:algún|algun|alguno|algunos)?\s*recordatorio(?:s)?\b", re.IGNORECASE),
    re.compile(r"\b(?:que|qué|cuáles?|cuales?)\s+recordatorio(?:s)?\s+(?:hay|tienes?|tengo)\b", re.IGNORECASE),
    re.compile(r"\bmis\s+recordatorio(?:s)?\b", re.IGNORECASE),
    re.compile(r"\blist(?:a|ar)?\s+(?:mis\s+)?recordatorio(?:s)?\b", re.IGNORECASE),
    re.compile(r"\brecordatorio(?:s)?\s+activos?\b", re.IGNORECASE),
    re.compile(r"\bshow\s+reminders?\b", re.IGNORECASE),
    re.compile(r"\blist\s+reminders?\b", re.IGNORECASE),
    re.compile(r"\bmy\s+reminders?\b", re.IGNORECASE),
    re.compile(r"\b(?:que|qué)\s+(?:me\s+)?(?:tienes?\s+que\s+)?recordar\b", re.IGNORECASE),
]


def _detect_monitor(text: str) -> list[SkillCall] | None:
    """Detect monitor creation requests. Returns SkillCall list or None."""
    for i, pattern in enumerate(_MONITOR_PATTERNS):
        m = pattern.search(text)
        if not m:
            continue

        raw_url = m.group(1)
        url = raw_url if raw_url.startswith("http") else f"https://{raw_url}"
        args: dict[str, str] = {"url": url, "monitor_type": "change"}

        # Pattern 0: keyword pattern (has keyword in group 2)
        if i == 0 and m.lastindex and m.lastindex >= 2:
            keyword = m.group(2).strip().strip("\"'")
            if keyword:
                args["keyword"] = keyword
                args["monitor_type"] = "keyword"

        # Pattern 2: general pattern with optional interval (groups 2, 3)
        if i == 2 and m.lastindex and m.lastindex >= 3:
            try:
                amount = int(m.group(2))
                unit_raw = m.group(3).lower()
                multiplier = 1
                for key, mult in _INTERVAL_UNIT_MAP.items():
                    if unit_raw.startswith(key[:3]):
                        multiplier = mult
                        break
                args["interval_minutes"] = str(amount * multiplier)
            except (TypeError, ValueError):
                pass

        return [SkillCall(
            skill_name="create_monitor",
            arguments=args,
            raw_text=f"[auto-detected: create monitor for {url}]",
        )]

    return None


# Shopping sites: alias → domain for site: operator search
# Using web_search(query="product site:domain") returns real indexed product/category pages
_SHOPPING_SITES: dict[str, str] = {
    "aliexpress": "aliexpress.com",
    "amazon": "amazon.com",
    "amazon.es": "amazon.es",
    "amazon.co.uk": "amazon.co.uk",
    "amazon.de": "amazon.de",
    "amazon.com.br": "amazon.com.br",
    "amazon.com.mx": "amazon.com.mx",
    "ebay": "ebay.com",
    "mercadolibre": "mercadolibre.cl",
    "mercado libre": "mercadolibre.cl",
    "mercadolibre.cl": "mercadolibre.cl",
    "mercadolibre.com.ar": "mercadolibre.com.ar",
    "mercadolibre.com.mx": "mercadolibre.com.mx",
    "mercadolibre.com.co": "mercadolibre.com.co",
    "pcfactory": "pcfactory.cl",
    "falabella": "falabella.com",
    "ripley": "ripley.cl",
    "paris": "paris.cl",
    "walmart": "walmart.com",
    "etsy": "etsy.com",
    "wish": "wish.com",
    "shein": "shein.com",
}

# Verbs that trigger shopping detection
_SHOP_VERBS_RE = r"(?:busca(?:r|me)?|encuentra(?:r|me)?|consigue(?:r|me)?|compra(?:r|me)?|find|search|get|buy)"

# Phrases to strip from the end of the query (after extracting product name)
_QUERY_TRAILING_RE = re.compile(
    r"\s+(?:y\s+)?(?:envía(?:me)?|manda(?:me)?|send|share|dame|dime|pasa(?:me)?|"
    r"muestra(?:me)?|trae(?:me)?|y\s+me\s+(?:envías?|mandas?|compartes?|dices?|muestras?|pasas?))"
    r"\b.*$",
    re.IGNORECASE,
)


_TASK_CREATE_RE = re.compile(
    r"\b(?:crea(?:r|me)?|programa(?:r)?|agrega(?:r)?|añade?|configura(?:r)?|establece?)\b"
    r".{0,30}"
    r"\b(?:tarea\s+programada|scheduled?\s+task|tarea\s+automática|job\s+programado|recordatorio\s+recurrente|tarea\s+periódica)\b",
    re.IGNORECASE,
)
_TASK_LIST_RE = re.compile(
    r"\b(?:lista(?:r|me)?|muestra(?:me)?|ver?|que(?:)\s+tareas?|mis\s+tareas?|tareas?\s+programadas?|scheduled?\s+tasks?)\b",
    re.IGNORECASE,
)
_TASK_DELETE_RE = re.compile(
    r"\b(?:elimina(?:r|me)?|borra(?:r)?|quita(?:r)?|desactiva(?:r)?)\s+(?:la\s+)?tarea\s+(.+)",
    re.IGNORECASE,
)


def _detect_task_management(text: str) -> SkillCall | None:
    """Detect task management intents."""
    # Skip detection for scheduled task messages — let LLM execute the instruction directly
    if text.startswith("[TAREA PROGRAMADA:"):
        return None
    if _TASK_CREATE_RE.search(text):
        # Let LLM handle creation with the skill (needs name + interval + instruction extraction)
        return SkillCall(
            skill_name="task_manager",
            arguments={"action": "create", "name": "", "instruction": "", "interval": ""},
            raw_text="[auto-detected: create scheduled task]",
        )
    if _TASK_LIST_RE.search(text) and re.search(r"\btarea", text, re.IGNORECASE):
        return SkillCall(
            skill_name="task_manager",
            arguments={"action": "list"},
            raw_text="[auto-detected: list tasks]",
        )
    m = _TASK_DELETE_RE.search(text)
    if m:
        return SkillCall(
            skill_name="task_manager",
            arguments={"action": "delete", "name": m.group(1).strip()},
            raw_text="[auto-detected: delete task]",
        )
    return None


# ---- Store price comparison detection ----------------------------------------
# Fired when user asks to compare prices across stores ("en que tienda",
# "tiendas chilenas", "precio en cada tienda", etc.) — builds a clean targeted
# query instead of passing the full raw text to web_search.

_STORE_COMPARE_RE = re.compile(
    r"\b(?:en\s+(?:qué|que)\s+tiendas?|en\s+tiendas?\s+(?:chilenas?|peruanas?|argentinas?|mexicanas?|colombianas?|locales?)|"
    r"precio\s+en\s+(?:cada|las?)\s+tienda|cada\s+(?:una|uno)\s+con\s+su\s+(?:link|url|precio)|"
    r"tiendas?\s+(?:chilenas?|peruanas?|argentinas?|mexicanas?|colombianas?))\b",
    re.IGNORECASE,
)

_COUNTRY_TERMS: dict[str, str] = {
    "chile": "Chile", "chilena": "Chile", "chilenas": "Chile", "chileno": "Chile", "chilenos": "Chile",
    "peru": "Peru", "perú": "Peru", "peruana": "Peru", "peruano": "Peru",
    "argentina": "Argentina", "argentino": "Argentina", "argentinas": "Argentina",
    "mexico": "Mexico", "méxico": "Mexico", "mexicana": "Mexico", "mexicano": "Mexico",
    "colombia": "Colombia", "colombiana": "Colombia",
    "brasil": "Brazil", "brazil": "Brazil", "brasileña": "Brazil",
    "usa": "USA", "estados unidos": "USA",
}


def _extract_product_from_text(text: str) -> str:
    """Extract a clean product name from a store comparison query."""
    # Strip leading search verbs
    cleaned = re.sub(
        r"^\s*(?:búscame|buscame|busca(?:me)?|encuéntrame|encuent(?:ra|r)(?:me)?|dame|dime|consígue(?:me)?)\s+",
        "", text, flags=re.IGNORECASE,
    )
    # Trim trailing meta-phrases about price/store/comparison
    cleaned = re.sub(
        r"\s+(?:en\s+(?:qué|que)\s+tienda|en\s+tiendas?|y\s+(?:qué|que)\s+precio|precio\s+en|"
        r"cada\s+(?:una|uno)|con\s+su\s+(?:link|url|precio)|y\s+sus?\s+(?:link|url|precio)).*$",
        "", cleaned, flags=re.IGNORECASE,
    ).strip()
    # Remove leading articles
    cleaned = re.sub(r"^(?:el|la|los|las|un|una|unos|unas)\s+", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip().rstrip("?!.,")


def _detect_store_comparison(text: str) -> SkillCall | None:
    """Detect multi-store price comparison queries and build a clean search query."""
    if not _STORE_COMPARE_RE.search(text):
        return None

    # Extract product name (needs a search verb to be present)
    product = ""
    if re.search(r"\bbusca(?:r|me)?", text, re.IGNORECASE):
        product = _extract_product_from_text(text)
    if not product or len(product) < 3:
        return None

    # Detect country modifier
    country = ""
    txt_lower = text.lower()
    for term, std_country in sorted(_COUNTRY_TERMS.items(), key=lambda x: -len(x[0])):
        if re.search(r"\b" + re.escape(term) + r"\b", txt_lower):
            country = std_country
            break

    # Build targeted query
    query = product
    if country and country.lower() not in product.lower():
        query += f" precio tiendas {country}"
    else:
        query += " precio tiendas"

    return SkillCall(
        skill_name="web_search",
        arguments={"query": query.strip(), "max_results": "10"},
        raw_text=f"[auto-detected: store comparison '{query[:60]}']",
    )


def _detect_shopping_search(text: str) -> SkillCall | None:
    """Detect shopping site searches.

    Navigates directly to the store's search URL via browser skill — no external
    search engine needed, works even when Google/Bing block automated requests.
    """
    text_lower = text.lower()

    # Find which shopping site is mentioned (multi-word first, then single-word)
    site_found = None
    url_template = None
    for site, tmpl in sorted(_SHOPPING_SITES.items(), key=lambda x: -len(x[0])):
        if site in text_lower:
            site_found = site
            url_template = tmpl
            break

    if not site_found or not url_template:
        return None

    # Make sure there's a search verb present (avoid "estoy en aliexpress")
    if not re.search(r"\b" + _SHOP_VERBS_RE + r"\b", text, re.IGNORECASE):
        return None

    # Extract product query — two strategies:
    # 1. "busca en [site] [product]"
    # 2. "busca [product] en [site]"
    site_escaped = re.escape(site_found)
    patterns = [
        re.compile(
            rf"\b{_SHOP_VERBS_RE}\s+en\s+{site_escaped}(?:\.(?:com|cl|es|de|co\.uk|com\.\w+))?\s+(.+)",
            re.IGNORECASE,
        ),
        re.compile(
            rf"\b{_SHOP_VERBS_RE}\s+(.+?)\s+en\s+{site_escaped}\b",
            re.IGNORECASE,
        ),
        re.compile(
            rf"\b{_SHOP_VERBS_RE}\s+(.+?)\s+(?:en|in|on)\s+{site_escaped}\b",
            re.IGNORECASE,
        ),
    ]

    query = ""
    for pat in patterns:
        m = pat.search(text)
        if m:
            query = m.group(1).strip()
            break

    if not query or len(query) < 2:
        return None

    # Clean trailing action phrases: "cajita musical y me envías los enlaces" → "cajita musical"
    query = _QUERY_TRAILING_RE.sub("", query).strip().rstrip("?!.,")

    if not query:
        return None

    # Strip leading articles (un, una, unos, unas, el, la, los, las)
    query = re.sub(r"^(?:un(?:os?|as?)?|el|la|los|las)\s+", "", query, flags=re.IGNORECASE).strip()

    if not query:
        return None

    # Use site: operator — returns real AliExpress/Amazon/etc. product/category pages
    # e.g., "cajita musical site:aliexpress.com" → es.aliexpress.com/w/wholesale-cajita-musical.html
    site_domain = url_template  # _SHOPPING_SITES now stores domains
    search_query = f"{query} site:{site_domain}"

    return SkillCall(
        skill_name="web_search",
        arguments={"query": search_query, "max_results": "8", "lang": "es-es"},
        raw_text=f"[auto-detected: shop search '{query}' on {site_found}]",
    )


def _detect_youtube_scrape(text: str) -> SkillCall | None:
    """Detect YouTube URLs and auto-trigger deep-scraper for transcript extraction."""
    m = _YOUTUBE_URL_PATTERN.search(text)
    if not m:
        return None
    video_id = m.group(1)
    clean_url = f"https://www.youtube.com/watch?v={video_id}"
    return SkillCall(
        skill_name="deep_scraper",
        arguments={"url": clean_url},
        raw_text=f"[auto-detected: deep_scraper YouTube {clean_url}]",
    )


def detect_skills(text: str) -> list[SkillCall]:
    """Detect which skills should be auto-executed for a user message.

    Returns a list of SkillCall objects ready for execution.
    """
    calls = []

    # Scheduled task messages must not trigger auto-detect — let the LLM execute the instruction
    if text.startswith("[TAREA PROGRAMADA:"):
        return []

    # Check for reminder patterns (highest priority — intercept before search)
    # Normalize number words ("un minuto" -> "1 minuto") for matching
    text_norm = _normalize_number_words(text)

    # Absolute time reminders: "avísame mañana a las 9am", "recuérdame hoy a las 15:00"
    # Time-only reminders: "avísame cuando sean las 8:30" / "avísame a las 8:30 AM"
    _TIME_ONLY_REM_RE = re.compile(
        _REM_VERBS
        + r"\s+(?:cuando\s+sean?\s+las?\s+|a\s+las?\s+|las?\s+)"
        r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)"
        r"(?:\s+(?:que|para|de)\s+(.+))?",
        re.IGNORECASE,
    )
    _tom = _TIME_ONLY_REM_RE.search(text)
    if _tom:
        time_ref = _tom.group(1).strip()
        reminder_text = (_tom.group(2) or "").strip().rstrip("?!.") or "Recordatorio"
        calls.append(SkillCall(
            skill_name="create_reminder",
            arguments={"text": reminder_text, "due": time_ref},
            raw_text=f"[auto-detected: time-only reminder {time_ref}]",
        ))
        return calls

    _ABS_REMINDER_PATTERNS = [
        # "avísame/recuérdame mañana/hoy/pasado mañana a las 9:00 que ..."
        re.compile(
            _REM_VERBS + r"\s+(?:el\s+)?"
            r"(mañana|manana|hoy|today|tomorrow|pasado\s+mañana|pasado\s+manana)"
            r"(?:\s+a\s+las?\s+|\s+at\s+|\s+)"
            r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)"
            r"(?:\s+(?:que|to|para|de)\s+(.+))?",
            re.IGNORECASE,
        ),
        # "avísame mañana a las 9" (without reminder text)
        re.compile(
            _REM_VERBS + r"\s+(?:el\s+)?"
            r"(mañana|manana|hoy|today|tomorrow|pasado\s+mañana|pasado\s+manana)"
            r"(?:\s+a\s+las?\s+|\s+at\s+|\s+)"
            r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)"
            r"\s*$",
            re.IGNORECASE,
        ),
        # "mañana a las 9 avísame/recuérdame que ..."
        re.compile(
            r"\b(mañana|manana|hoy|today|tomorrow|pasado\s+mañana|pasado\s+manana)"
            r"(?:\s+a\s+las?\s+|\s+at\s+|\s+)"
            r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s+"
            + _REM_VERBS + r"(?:\s+(?:que|to|para|de)\s+(.+))?",
            re.IGNORECASE,
        ),
    ]
    for pattern in _ABS_REMINDER_PATTERNS:
        m = pattern.search(text)
        if m:
            day_ref = m.group(1) if not m.group(1) in (None, "") else "mañana"
            time_ref = m.group(2)
            reminder_text = ""
            if m.lastindex and m.lastindex >= 3 and m.group(3):
                reminder_text = m.group(3).strip().rstrip("?!.")
            if not reminder_text:
                reminder_text = "Recordatorio"
            # Build due string for _parse_due
            due = f"{day_ref} {time_ref}"
            calls.append(SkillCall(
                skill_name="create_reminder",
                arguments={"text": reminder_text, "due": due},
                raw_text=f"[auto-detected: reminder {due}]",
            ))
            return calls

    # Relative time reminders: "recuérdame en 5 minutos que ..."
    for pattern in _REMINDER_PATTERNS:
        m = pattern.search(text_norm)
        if m:
            amount = m.group(1)
            raw_unit = m.group(2).lower()
            # group(3) may be None for patterns without reminder text
            reminder_text = (m.group(3) or "").strip().rstrip("?!.") or "Recordatorio"
            unit = _TIME_UNIT_MAP.get(raw_unit, "m")
            calls.append(SkillCall(
                skill_name="create_reminder",
                arguments={"text": reminder_text, "due": f"+{amount}{unit}"},
                raw_text=f"[auto-detected: reminder in {amount}{unit}]",
            ))
            return calls

    # Check for monitor LIST queries (highest priority — bypass LLM)
    for pattern in _MONITOR_LIST_PATTERNS:
        if pattern.search(text):
            return [SkillCall(
                skill_name="list_monitors",
                arguments={},
                raw_text="[auto-detected: list monitors]",
            )]

    # Check for reminder LIST queries (bypass LLM)
    for pattern in _REMINDER_LIST_PATTERNS:
        if pattern.search(text):
            return [SkillCall(
                skill_name="list_reminders",
                arguments={},
                raw_text="[auto-detected: list reminders]",
            )]

    # Detect "wipe/delete all" requests — trigger when 2+ of {tasks, goals, agents} appear
    # together with a delete verb (avoids false positive on single-entity delete)
    _wipe_has_verb = bool(re.search(
        r"\b(?:elimina(?:r)?|borra(?:r)?|delete|limpia(?:r)?|quita(?:r)?|remove)\b",
        text, re.IGNORECASE,
    ))
    if _wipe_has_verb:
        _wipe_kw_count = sum([
            bool(re.search(r"\b(?:tareas?|tasks?)\b", text, re.IGNORECASE)),
            bool(re.search(r"\bgoals?\b", text, re.IGNORECASE)),
            bool(re.search(r"\b(?:agentes?|agents?)\b", text, re.IGNORECASE)),
        ])
        if _wipe_kw_count >= 2:
            return [SkillCall(
                skill_name="agent_manager",
                arguments={"action": "wipe_all"},
                raw_text="[auto-detected: wipe all tasks/goals/agents]",
            )]

    # Detect email SEND requests — intercept before LLM falls back to web_search
    # Veto: don't auto-detect gmail if message is a complex task that merely
    # mentions an email address as a destination (screenshots, reports, monitoring).
    _EMAIL_SEND_RE = re.compile(
        r"\b(?:env[ií]a(?:r|lo|la|me|nos)?|manda(?:r|lo|la|me)?|send|remite)\b"
        r".*?\b(?:a|al|to)\b.*?[\w.\-]+@[\w.\-]+\.\w+",
        re.IGNORECASE,
    )
    _EMAIL_COMPLEX_TASK_VETO = re.compile(
        r"\b(?:captur[a-z]*|screenshot|informe|reporte|monitorea|monitoriza|"
        r"adjunt[a-z]*|browser|precio|foto|imagen|binance|crypto|cripto|"
        r"paso\s+\d|PASO\s+\d)\b",
        re.IGNORECASE,
    )
    if _EMAIL_SEND_RE.search(text) and not _EMAIL_COMPLEX_TASK_VETO.search(text):
        # Extract recipient from text and lock it to prevent parameter mutation
        _addr_m = re.search(r'[\w.\-]+@[\w.\-]+\.\w+', text)
        _locked_to = _addr_m.group(0) if _addr_m else ""
        _send_args: dict = {"action": "send_check"}
        if _locked_to:
            _send_args["_locked_to"] = _locked_to  # propagated to send_check for immutability
        return [SkillCall(
            skill_name="gmail",
            arguments=_send_args,
            raw_text="[auto-detected: email send request]",
        )]

    # Check for Gmail inbox patterns (before search to avoid false match)
    _GMAIL_INBOX_PATTERNS = [
        re.compile(r"\b(?:revisa|lee|mira|check|read|show)\b.*\b(?:mi\s+)?(?:correo|email|inbox|bandeja|mail|gmail)\b", re.IGNORECASE),
        re.compile(r"\b(?:mi\s+)?(?:correo|email|inbox|bandeja|mail|gmail)\b.*\b(?:revisa|lee|mira|check|read|show)\b", re.IGNORECASE),
        re.compile(r"\b(?:tengo|hay)\s+(?:algún|algun|nuevo|nuevos)?\s*(?:correo|email|mail)s?\b", re.IGNORECASE),
        re.compile(r"\bmis\s+(?:correo|email|mail)s?\b", re.IGNORECASE),
        re.compile(r"\b(?:que|qué)\s+(?:correo|email|mail)s?\s+(?:tengo|hay)\b", re.IGNORECASE),
    ]
    for pattern in _GMAIL_INBOX_PATTERNS:
        if pattern.search(text):
            return [SkillCall(
                skill_name="gmail",
                arguments={"action": "inbox", "count": "10"},
                raw_text="[auto-detected: gmail inbox]",
            )]

    # Check for Gmail search patterns
    _GMAIL_SEARCH_RE = re.compile(
        r"\b(?:busca|buscar|encuentra|search)\b.*\b(?:correo|email|mail)s?\b.*?\b(?:de|sobre|from|about)\s+(.+)",
        re.IGNORECASE,
    )
    gmail_search_m = _GMAIL_SEARCH_RE.search(text)
    if gmail_search_m:
        query = gmail_search_m.group(1).strip().rstrip("?!.")
        if query:
            return [SkillCall(
                skill_name="gmail",
                arguments={"action": "search", "query": query},
                raw_text=f"[auto-detected: gmail search '{query}']",
            )]

    # Check for skill management queries (list/enable/disable)
    _SKILL_LIST_PATTERNS = [
        re.compile(r"\b(?:que|qué|cuáles?|cuales?|lista|list)\b.*\bskills?\b", re.IGNORECASE),
        re.compile(r"\bskills?\s+(?:que\s+)?(?:tengo|tienes?|hay|activ[ao]s?|disponibles?)\b", re.IGNORECASE),
        re.compile(r"\bmis\s+skills?\b", re.IGNORECASE),
        re.compile(r"\bshow\s+skills?\b", re.IGNORECASE),
        re.compile(r"\b(?:todas?|all)\s+(?:las?\s+)?skills?\b", re.IGNORECASE),
        re.compile(r"\bhabilidades\b", re.IGNORECASE),
    ]
    for pattern in _SKILL_LIST_PATTERNS:
        if pattern.search(text):
            return [SkillCall(
                skill_name="skill_manager",
                arguments={"action": "list"},
                raw_text="[auto-detected: list skills]",
            )]

    # Skill enable/disable
    _SKILL_TOGGLE_RE = re.compile(
        r"\b(activa|desactiva|enable|disable|enciende|apaga|prende)\b"
        r".*?\b(?:el\s+)?(?:skill|habilidad)\s+(\w[\w_-]*)",
        re.IGNORECASE,
    )
    toggle_m = _SKILL_TOGGLE_RE.search(text)
    if toggle_m:
        verb = toggle_m.group(1).lower()
        skill_name = toggle_m.group(2).strip()
        action = "disable" if verb in ("desactiva", "disable", "apaga") else "enable"
        return [SkillCall(
            skill_name="skill_manager",
            arguments={"action": action, "name": skill_name},
            raw_text=f"[auto-detected: {action} skill {skill_name}]",
        )]

    # Also match "desactiva X" / "activa X" with "skill" after
    _SKILL_TOGGLE_RE2 = re.compile(
        r"\b(?:el\s+)?(?:skill|habilidad)\s+(\w[\w_-]*)\s+"
        r"(activa|desactiva|enable|disable|activalo|desactivalo)",
        re.IGNORECASE,
    )
    toggle_m2 = _SKILL_TOGGLE_RE2.search(text)
    if toggle_m2:
        skill_name = toggle_m2.group(1).strip()
        verb = toggle_m2.group(2).lower()
        action = "disable" if "desactiv" in verb or verb == "disable" else "enable"
        return [SkillCall(
            skill_name="skill_manager",
            arguments={"action": action, "name": skill_name},
            raw_text=f"[auto-detected: {action} skill {skill_name}]",
        )]

    # Skill edit/modify
    _SKILL_EDIT_RE = re.compile(
        r"\b(?:edita|modifica|cambia|update|edit|modify)\b"
        r".*?\b(?:el\s+)?(?:skill|habilidad)\s+(\w[\w_-]*)",
        re.IGNORECASE,
    )
    edit_m = _SKILL_EDIT_RE.search(text)
    if edit_m:
        skill_name = edit_m.group(1).strip()
        return [SkillCall(
            skill_name="skill_manager",
            arguments={"action": "edit", "name": skill_name},
            raw_text=f"[auto-detected: edit skill {skill_name}]",
        )]

    # Also match "skill X editalo/modificalo"
    _SKILL_EDIT_RE2 = re.compile(
        r"\b(?:el\s+)?(?:skill|habilidad)\s+(\w[\w_-]*)\s+"
        r"(?:editalo|modificalo|cambialo|update|edit)",
        re.IGNORECASE,
    )
    edit_m2 = _SKILL_EDIT_RE2.search(text)
    if edit_m2:
        skill_name = edit_m2.group(1).strip()
        return [SkillCall(
            skill_name="skill_manager",
            arguments={"action": "edit", "name": skill_name},
            raw_text=f"[auto-detected: edit skill {skill_name}]",
        )]

    # Skill deletion
    _SKILL_DELETE_RE = re.compile(
        r"\b(?:elimina|borra|delete|remove|quita)\b.*?\b(?:el\s+)?(?:skill|habilidad)\s+(\w[\w_-]*)",
        re.IGNORECASE,
    )
    del_m = _SKILL_DELETE_RE.search(text)
    if del_m:
        skill_name = del_m.group(1).strip()
        return [SkillCall(
            skill_name="skill_manager",
            arguments={"action": "delete", "name": skill_name},
            raw_text=f"[auto-detected: delete skill {skill_name}]",
        )]

    # Check for monitor creation patterns (before datetime/search to catch "monitorea URL")
    monitor_match = _detect_monitor(text)
    if monitor_match:
        return monitor_match

    # Check for datetime patterns
    for pattern in _DATETIME_PATTERNS:
        if pattern.search(text):
            location = _extract_location(text)
            tz = _resolve_timezone(location) if location else "UTC"
            calls.append(SkillCall(
                skill_name="get_datetime",
                arguments={"tz": tz},
                raw_text=f"[auto-detected: datetime for {tz}]",
            ))
            break

    # Check for weather patterns
    for pattern in _WEATHER_PATTERNS:
        if pattern.search(text):
            location = _extract_location(text)
            if location:
                calls.append(SkillCall(
                    skill_name="get_weather",
                    arguments={"city": location},
                    raw_text=f"[auto-detected: weather for {location}]",
                ))
            break

    # Scrape detection: when user mentions news/articles + a site reference.
    # Only fires when the user gives an explicit URL or a domain with TLD.
    # Aliases ("emol", "bbc") are NOT mapped to hardcoded URLs anymore —
    # the LLM resolves them through web_search.
    def _find_site_in_text(txt: str) -> tuple[str | None, str]:
        """Find a site reference in text. Returns (url, site_name) or (None, '')."""
        url_m = _URL_PATTERN.search(txt)
        if url_m:
            return url_m.group(1), url_m.group(1)
        dom_m = _DOMAIN_PATTERN.search(txt)
        if dom_m:
            return f"https://{dom_m.group(1)}", dom_m.group(1)
        return None, ""

    # News/content keywords
    _NEWS_RE = re.compile(
        r"\b(?:noticias?|articulos?|artículos?|news|headlines?|titulares?|portada)\b",
        re.IGNORECASE,
    )

    # Detect: text has news keyword + site reference
    has_news_kw = _NEWS_RE.search(text)
    site_url, site_name = _find_site_in_text(text)

    if has_news_kw and site_url:
        # Extract topic: look for "de/sobre TOPIC" patterns anywhere in text
        topic = ""
        # Multiple topic extraction patterns
        _TOPIC_PATTERNS = [
            # "noticias de TOPIC" / "noticias sobre TOPIC"
            re.compile(r"\bnoticias?\s+(?:de|sobre|del?|acerca\s+de)\s+(.+?)(?:\s+(?:en|y|de)\s+|\s*[?]?\s*$)", re.IGNORECASE),
            # "TOPIC en SITE" / "sobre TOPIC" (after site name)
            re.compile(r"\bsobre\s+(?:el\s+|la\s+|los\s+|las\s+)?(.+?)(?:\s*[?]?\s*$)", re.IGNORECASE),
            # "si hay ... de TOPIC"
            re.compile(r"\bsi\s+hay\s+(?:alguna?\s+)?(?:noticia|articulo|artículo)\s+(?:de|sobre)\s+(.+?)(?:\s+(?:y|en)\s+|\s*[?]?\s*$)", re.IGNORECASE),
        ]

        for tp in _TOPIC_PATTERNS:
            tm = tp.search(text)
            if tm:
                candidate = tm.group(1).strip().rstrip("?!.,")
                # Remove determiners
                candidate = re.sub(r"^(?:alguna?|algún|alguno|una?|el|la|los|las)\s+", "", candidate, flags=re.IGNORECASE)
                candidate = candidate.strip()
                # Strip site name from topic if it leaked in (e.g. "emol sobre Trump" -> "Trump")
                if site_name:
                    candidate = re.sub(
                        r"\b" + re.escape(site_name) + r"\b\s*(?:sobre|de|en)?\s*",
                        "", candidate, flags=re.IGNORECASE,
                    ).strip()
                # Don't use the site name as topic
                if candidate.lower() != site_name.lower() and len(candidate) > 1:
                    topic = candidate
                    break

        args: dict[str, str] = {"url": site_url, "max_results": "10"}
        if topic:
            args["keyword"] = topic
        calls.append(SkillCall(
            skill_name="scrape",
            arguments=args,
            raw_text=f"[auto-detected: scrape {site_url}" + (f" keyword={topic}]" if topic else "]"),
        ))
        return calls

    # Also detect: "dime las noticias de SITE" (site after "noticias de")
    if has_news_kw and not site_url:
        # Try to find site after "noticias de/en"
        m = re.search(r"\bnoticias?\s+(?:de|en)\s+(\w+)", text, re.IGNORECASE)
        if m:
            candidate = m.group(1).strip()
            resolved, _ = _find_site_in_text(candidate)
            if resolved:
                calls.append(SkillCall(
                    skill_name="scrape",
                    arguments={"url": resolved, "max_results": "15"},
                    raw_text=f"[auto-detected: scrape {resolved}]",
                ))
                return calls

    # ── browser_smart_navigate auto-intent ───────────────────────────────────
    # Triggers: "navega", "scroll inteligente", "recorre la página",
    # "baja hasta el final", "carga todo el contenido", "navega paso a paso"
    _SMART_NAV_PATTERN = re.compile(
        r"\b(?:"
        # Spanish
        r"navega(?:r)?\s+(?:paso\s+a\s+paso|inteligente(?:mente)?|(?:hasta\s+)?(?:el\s+)?final)|"
        r"scroll\s+inteligente|"
        r"recorre(?:r)?\s+(?:la\s+)?p[aá]gina|"
        r"baja(?:r)?\s+hasta\s+(?:el\s+)?final|"
        r"carga(?:r)?\s+todo\s+el\s+contenido|"
        r"navega(?:r)?\s+(?:la\s+p[aá]gina|el\s+sitio)\s+completa?o?|"
        # English
        r"smart\s+navigate|"
        r"navigate\s+(?:step[\s-]by[\s-]step|intelligently|to\s+the\s+(?:bottom|end))|"
        r"scroll\s+(?:to\s+the\s+)?(?:bottom|end)\s+of|"
        r"load\s+all\s+(?:the\s+)?content|"
        r"scroll\s+(?:down\s+)?(?:and\s+)?(?:load|fetch)\s+(?:everything|all)|"
        r"browse\s+the\s+(?:full\s+)?page|"
        # Portuguese
        r"navegar?\s+(?:passo\s+a\s+passo|at[eé]\s+o\s+final)|"
        r"rolar?\s+at[eé]\s+o\s+final|"
        r"carregar?\s+todo\s+o\s+conte[uú]do|"
        # French
        r"naviguer?\s+pas\s+à\s+pas|"
        r"faire\s+d[eé]filer?\s+jusqu'?(?:au\s+bas|à\s+la\s+fin)|"
        # German
        r"(?:seite\s+)?bis\s+zum\s+ende\s+scrollen|"
        r"intelligente[sr]?\s+navigation"
        r")\b",
        re.IGNORECASE,
    )
    _SMART_NAV_URL_RE = re.compile(
        r"(https?://[^\s]+|[a-zA-Z0-9][-a-zA-Z0-9]*\.(?:com|org|net|io|dev|ai|app|co|cl|ar|mx|es|uk|de|fr|br|xyz|me|info|tech|pro|site|online|store|shop)(?:/[^\s]*)?)",
        re.IGNORECASE,
    )
    if _SMART_NAV_PATTERN.search(text):
        _sn_url_m = _SMART_NAV_URL_RE.search(text)
        _sn_url = _normalize_browser_url(_sn_url_m.group(1)) if _sn_url_m else None
        if _sn_url:
            # Detect if user wants load-more clicking (default: yes)
            _no_click = re.search(r"\b(?:sin\s+click|no\s+click|solo\s+scroll|only\s+scroll)\b", text, re.IGNORECASE)
            calls.append(SkillCall(
                skill_name="browser_smart_navigate",
                arguments={
                    "url": _sn_url,
                    "session": "nav1",
                    "click_load_more": "false" if _no_click else "true",
                    "wait_ms": "500",
                    "capture": "true",
                },
                raw_text=f"[auto-detected: browser_smart_navigate {_sn_url}]",
            ))
            return calls

    # ── browser_screenshot_full_page auto-intent ─────────────────────────────
    # Triggers: "captura completa", "haz scroll y captura", "todo el sitio",
    # "full page screenshot", "captura toda la página"
    _FULL_PAGE_SCREENSHOT_PATTERN = re.compile(
        r"\b(?:"
        # Spanish
        r"captura\s+(?:completa|toda\s+la\s+p[aá]gina|p[aá]gina\s+completa|completo)|"
        r"captura\s+(?:este|el|ese|un)\s+\w+\s+(?:completo|completa)|"
        r"captura\s+completo|"
        r"haz\s+scroll\s+y\s+(?:captura|screenshot)|"
        r"todo\s+el\s+(?:sitio|contenido|p[aá]gina|portal)|"
        r"p[aá]gina\s+entera|captura\s+entera|screenshot\s+completo|"
        r"scroll\s+y\s+(?:capture|captura|screenshot)|"
        r"captura(?:r)?\s+.{0,40}\s+haciendo\s+scroll|"
        r"haciendo\s+scroll\s+.{0,40}\s+captura(?:s)?|"
        # English
        r"full[\s-]page\s+screenshot|"
        r"screenshot\s+(?:of\s+the\s+)?(?:full|entire|whole|complete)\s+(?:page|site|website)|"
        r"capture\s+(?:the\s+)?(?:full|entire|whole|complete)\s+(?:page|site|website)|"
        r"scroll\s+(?:and|&)\s+(?:capture|screenshot)|"
        r"capture\s+.{0,40}\s+(?:scrolling|with\s+scroll)|"
        r"(?:scrolling|scroll\s+down)\s+.{0,40}\s+capture|"
        r"take\s+(?:a\s+)?(?:full|complete)\s+(?:page\s+)?screenshot|"
        # Portuguese
        r"captura\s+(?:completa|toda\s+a\s+p[aá]gina|p[aá]gina\s+completa)|"
        r"tirar?\s+screenshot\s+(?:completo|da\s+p[aá]gina\s+(?:inteira|toda))|"
        r"rolar?\s+e\s+capturar?|"
        # French
        r"capture\s+(?:compl[eè]te|de\s+toute\s+la\s+page|page\s+enti[eè]re)|"
        r"faire\s+(?:une\s+)?capture\s+(?:d[eu])\s+(?:la\s+)?page\s+(?:enti[eè]re|compl[eè]te)|"
        # German
        r"vollst[aä]ndiger?\s+screenshot|"
        r"screenshot\s+(?:der\s+)?(?:ganzen?|gesamten?|vollst[aä]ndigen?)\s+(?:seite|webseite)|"
        r"seite\s+(?:komplett|vollst[aä]ndig)\s+(?:scrollen\s+und\s+)?(?:aufnehmen|screenshoten)"
        r")\b",
        re.IGNORECASE,
    )
    _FULL_PAGE_URL_RE = re.compile(
        r"(https?://[^\s]+|[a-zA-Z0-9][-a-zA-Z0-9]*\.(?:com|org|net|io|dev|ai|app|co|cl|ar|mx|es|uk|de|fr|br|xyz|me|info|tech|pro|site|online|store|shop)(?:/[^\s]*)?)",
        re.IGNORECASE,
    )
    if _FULL_PAGE_SCREENSHOT_PATTERN.search(text):
        _fp_url_m = _FULL_PAGE_URL_RE.search(text)
        _fp_url = _normalize_browser_url(_fp_url_m.group(1)) if _fp_url_m else None
        if _fp_url:
            calls.append(SkillCall(
                skill_name="browser_screenshot_full_page",
                arguments={"url": _fp_url, "session": "fullpage1", "wait_ms": "500"},
                raw_text=f"[auto-detected: browser_screenshot_full_page {_fp_url}]",
            ))
            return calls

    # ── browser_deep_scrape auto-intent ──────────────────────────────────────
    # Triggers: "extrae información de", "analiza el contenido de", "scrapea"
    _DEEP_SCRAPE_PATTERN = re.compile(
        r"\b(?:"
        # Spanish
        r"extra[eé]\s+(?:la\s+)?(?:informaci[oó]n|contenido|datos?|texto|art[ií]culo)|"
        r"analiza\s+(?:el\s+)?(?:contenido|p[aá]gina|sitio|art[ií]culo)|"
        r"scrapea(?:r)?|"
        r"scrape?\s+profundo|scraping\s+profundo|scrapeo\s+profundo|"
        r"dame\s+(?:el\s+)?(?:contenido|art[ií]culo|texto)\s+de|"
        r"extrae\s+el\s+art[ií]culo|"
        r"analiza\s+(?:el\s+)?(?:sitio|web)|"
        # English
        r"deep\s+scrape|"
        r"scrape\s+(?:the\s+)?(?:content|page|site|article)|"
        r"extract\s+(?:the\s+)?(?:content|information|text|article)\s+(?:from|of)|"
        r"analyze\s+(?:the\s+)?(?:content|page|site|article)\s+(?:at|from|of|on)|"
        r"get\s+(?:the\s+)?(?:content|text|article)\s+(?:from|of)\s+(?:this\s+)?(?:page|site|url)|"
        r"parse\s+(?:the\s+)?(?:page|site|content)|"
        # Portuguese
        r"extrai[ar]?\s+(?:o\s+)?(?:conte[uú]do|informa[cç][oã]o|texto|artigo)\s+(?:d[eoa]|em)|"
        r"analisar?\s+(?:o\s+)?(?:conte[uú]do|p[aá]gina|site|artigo)|"
        r"scrap(?:ear?|ing)\s+(?:o\s+)?(?:conte[uú]do|site)|"
        # French
        r"extraire?\s+(?:le\s+)?(?:contenu|l'article|les\s+informations)\s+(?:de|du|d')|"
        r"analyser?\s+(?:le\s+)?(?:contenu|la\s+page|le\s+site)|"
        # German
        r"inhalt\s+(?:extrahieren|scrapen|auslesen)\s+(?:von|der|des)|"
        r"(?:web)?seite\s+(?:scrapen|auslesen|analysieren)"
        r")\b",
        re.IGNORECASE,
    )
    _DEEP_SCRAPE_URL_RE = re.compile(
        r"(https?://[^\s]+|[a-zA-Z0-9][-a-zA-Z0-9]*\.(?:com|org|net|io|dev|ai|app|co|cl|ar|mx|es|uk|de|fr|br|xyz|me|info|tech|pro|site|online|store|shop)(?:/[^\s]*)?)",
        re.IGNORECASE,
    )
    if _DEEP_SCRAPE_PATTERN.search(text):
        _ds_url_m = _DEEP_SCRAPE_URL_RE.search(text)
        _ds_url = _normalize_browser_url(_ds_url_m.group(1)) if _ds_url_m else None
        if _ds_url:
            calls.append(SkillCall(
                skill_name="browser_deep_scrape",
                arguments={"url": _ds_url, "session": "scrape1"},
                raw_text=f"[auto-detected: browser_deep_scrape {_ds_url}]",
            ))
            return calls

    # Veto scroll_capture when user explicitly says NOT to scroll
    _NO_SCROLL_VETO = re.compile(
        r"\b(?:no\s+(?:hagas?\s+)?scroll|sin\s+scroll|no\s+scroll|without\s+scroll|"
        r"no\s+desplaz|no\s+bajar?|no\s+desliz)\b",
        re.IGNORECASE,
    )

    # Scroll + capture (multilingual catch-all)
    _SCROLL_CAPTURE_PATTERN = re.compile(
        # scroll-word ... capture-word
        r"\b(?:scroll|navega\s+(?:hacia\s+)?abajo|desplaz[aá]|bajar?|desliz[aá]|haciendo\s+scroll|"
        r"scrolling|scroll\s+down|rolar?|d[eé]filer?|scrollen)\b.{0,60}"
        r"\b(?:capturas?|screenshot|foto|imagen|pantalla|capture|captures?|photo|snap)\b"
        r"|"
        # capture-word ... scroll/complete-word
        r"\b(?:capturas?|screenshot|foto|imagen|pantalla|capture|captures?|photo|snap)\b.{0,60}"
        r"\b(?:scroll|haciendo\s+scroll|scrolling|scroll\s+down|mientras\s+(?:bajas?|navegas?)|"
        r"(?:hacia\s+)?abajo|completa?|todo\s+el\s+sitio|completo|"
        r"rolar?|d[eé]filer?|scrollen|while\s+scrolling|as\s+(?:you\s+)?scroll)\b",
        re.IGNORECASE,
    )
    _SCROLL_URL_EXTRACT = re.compile(r"(https?://[^\s]+|[a-zA-Z0-9][-a-zA-Z0-9]*\.(?:com|org|net|io|dev|ai|app|co|cl|ar|mx|es|uk|de|fr|br|xyz|me|info|tech|pro|site|online|store|shop)(?:/[^\s]*)?)", re.IGNORECASE)
    if _SCROLL_CAPTURE_PATTERN.search(text) and not _NO_SCROLL_VETO.search(text):
        _su = _SCROLL_URL_EXTRACT.search(text)
        # Auto-fire only when the user explicitly gives a URL or domain.
        # Site-name resolution (e.g. "la segunda") goes through the LLM +
        # web_search flow — no hardcoded name→URL shortcuts.
        if _su:
            _scroll_url = _normalize_browser_url(_su.group(1))
            calls.append(SkillCall(
                skill_name="browser_screenshot_full_page",
                arguments={"url": _scroll_url, "session": "fullpage1", "wait_ms": "500"},
                raw_text=f"[auto-detected: browser_screenshot_full_page {_scroll_url}]",
            ))
            return calls

    # If user provides alternative URL after failed screenshot: "intenta desde
    # esta url: https://...". Retry-only phrasings — bare "captura" was
    # previously included here, which made this pattern eat multi-URL
    # screenshot requests like `captura X y Y` (matched the LAST URL only).
    # Restricted to explicit retry verbs.
    _RETRY_URL_PATTERN = re.compile(
        r"\b(?:intenta\s+(?:con|desde|de\s+nuevo)|prueba\s+(?:con|desde)|"
        r"usa\s+(?:esta|el|la)|desde\s+esta|con\s+esta)\b.{0,40}"
        r"(https?://[^\s]+)",
        re.IGNORECASE,
    )
    retry_url_match = _RETRY_URL_PATTERN.search(text)
    if retry_url_match:
        url = _normalize_browser_url(retry_url_match.group(1).rstrip(".,;"))
        calls.append(SkillCall(
            skill_name="browser",
            arguments={"action": "capture", "url": url, "session": "s1"},
            raw_text=f"[auto-detected: browser capture retry {url}]",
        ))
        return calls

    # If user asks for screenshot/capture of a site, auto-execute browser directly
    _SCREENSHOT_PATTERN = re.compile(
        r"\b(?:captura|screenshot|pantallazo|foto|imagen)\b.*?"
        r"(?:(?:de(?:l)?|of|from)\s+)?(?:(?:la\s+)?(?:pagina|página|web|sitio|site)\s+)?"
        r"(https?://[^\s]+|[a-zA-Z0-9][-a-zA-Z0-9]*\.(?:com|org|net|io|dev|ai|app|co|cl|ar|mx|es|uk|de|fr|br|xyz|me|info|tech|pro|site|online|store|shop)(?:/[^\s]*)?)",
        re.IGNORECASE,
    )
    # Named-site screenshot: "captura de El Mercurio", "screenshot del New York Times"
    # Also: "portada del diario La Segunda", "envíame la portada de La Tercera" (portada implies screenshot)
    # Matches when user says captura/screenshot OR portada + de + site name (no URL required)
    _SCREENSHOT_NAMED_PATTERN = re.compile(
        r"\b(?:captura(?:r|me)?|screenshot|pantallazo|foto|imagen|portada)\b.{0,60}"
        r"\b(?:de(?:l)?|of|from|a)\b\s+"
        r"(?:(?:la|el|los|las)\s+)?(?:p[aá]gina\s+|web\s+|portada\s+(?:de(?:l)?\s+)?|sitio\s+(?:de(?:l)?\s+)?|diario\s+)?"
        r"([a-zA-ZáéíóúñÁÉÍÓÚÑ][a-zA-ZáéíóúñÁÉÍÓÚÑ0-9 ]{1,40})",
        re.IGNORECASE,
    )
    _BROWSE_PATTERN = re.compile(
        r"\b(?:entra|abre|visita|navega|mira|revisa|lee|browse|open|visit|go\s+to|check|look\s+at|see)\b.*?"
        r"(https?://[^\s]+|[a-zA-Z0-9][-a-zA-Z0-9]*\.(?:com|org|net|io|dev|ai|app|co|cl|ar|mx|es|uk|de|fr|br|xyz|me|info|tech|pro|site|online|store|shop)(?:/[^\s]*)?)",
        re.IGNORECASE,
    )

    # Screenshot request with explicit URL/domain — check ALL URLs in the
    # message, not just the first. Without this, `captura X y Y` only ever
    # captured X and silently dropped Y.
    screenshot_match = _SCREENSHOT_PATTERN.search(text)
    if screenshot_match:
        # Extract every URL/bare-domain after the screenshot verb
        _all_urls = []
        _url_re_loose = re.compile(
            r"(https?://[^\s]+|[a-zA-Z0-9][-a-zA-Z0-9]*\.(?:com|org|net|io|dev|ai|app|co|cl|ar|mx|es|uk|de|fr|br|xyz|me|info|tech|pro|site|online|store|shop)(?:/[^\s]*)?)",
            re.IGNORECASE,
        )
        for m in _url_re_loose.finditer(text):
            u = _normalize_browser_url(m.group(1).rstrip(".,;)"))
            if u not in _all_urls:
                _all_urls.append(u)
        # Cap at 5 URLs per request to bound resource use
        for idx, url in enumerate(_all_urls[:5]):
            calls.append(SkillCall(
                skill_name="browser",
                arguments={"action": "capture", "url": url, "session": f"s{idx+1}"},
                raw_text=f"[auto-detected: browser capture {url} ({idx+1}/{len(_all_urls)})]",
            ))
        return calls

    # Screenshot request with named site (no URL) — never hardcode a URL
    # for a name. Always resolve via web_search; the LLM will navigate to
    # the official URL on the next round.
    named_match = _SCREENSHOT_NAMED_PATTERN.search(text)
    if named_match:
        site_name = named_match.group(1).strip().lower()
        calls.append(SkillCall(
            skill_name="web_search",
            arguments={"query": f"sitio oficial {site_name}", "max_results": "3"},
            raw_text=f"[auto-detected: search for URL of '{site_name}' before screenshot]",
        ))
        return calls

    # Browse/open request: navigate only (return directly, skip LLM)
    browse_match = _BROWSE_PATTERN.search(text)
    if browse_match:
        raw_url = browse_match.group(1)
        url = raw_url if raw_url.startswith("http") else f"https://{raw_url}"
        calls.append(SkillCall(
            skill_name="browser",
            arguments={"action": "navigate", "url": url},
            raw_text=f"[auto-detected: browser navigate {url}]",
        ))
        return calls

    # Custom scheduled tasks: "crea una tarea programada que cada día..." → task_manager
    task_call = _detect_task_management(text)
    if task_call:
        return [task_call]

    # Multi-store price comparison: "busca X en que tienda", "precio en tiendas chilenas"
    # Must run BEFORE single-store shopping detect to build a clean targeted query
    store_compare_call = _detect_store_comparison(text)
    if store_compare_call:
        return [store_compare_call]

    # Shopping site search: "busca en aliexpress cajita musical" → web_search with site:
    shopping_call = _detect_shopping_search(text)
    if shopping_call:
        return [shopping_call]

    # General "busca donde comprar X más barato en Chile" → web_search (no specific site)
    _GENERAL_BUY_RE = re.compile(
        r"\b(?:busca(?:r|me)?|encuentra(?:r|me)?|consigue(?:r|me)?)\b.{0,40}"
        r"\b(?:donde\s+(?:puedo\s+)?comprar|m[aá]s\s+barato|precio(?:s?)\s+de)\b",
        re.IGNORECASE,
    )
    if _GENERAL_BUY_RE.search(text):
        # Use the whole text as search query (trimmed)
        query = text.strip().rstrip("?!.")
        return [SkillCall(
            skill_name="web_search",
            arguments={"query": query, "max_results": "10"},
            raw_text=f"[auto-detected: general buy search '{query[:50]}']",
        )]

    # YouTube URLs -> deep-scraper (intercept before generic fetch_url)
    youtube_call = _detect_youtube_scrape(text)
    if youtube_call:
        return [youtube_call]

    # Check for full URLs -> auto fetch_url
    urls = _extract_urls(text)
    if urls:
        for url in urls[:2]:
            calls.append(SkillCall(
                skill_name="fetch_url",
                arguments={"url": url, "max_chars": "3000"},
                raw_text=f"[auto-detected: fetch {url}]",
            ))
        return calls

    # Check for bare domains (nexocore.dev, etc.)
    # Use scrape if content-related words present, else fetch + search
    # Strip email addresses first to avoid false positives (e.g. "example.com" in "user@example.com")
    _text_no_emails = re.sub(r'[\w.\-]+@[\w.\-]+\.\w+', 'EMAIL_REDACTED', text)
    domain_match = _DOMAIN_PATTERN.search(_text_no_emails)
    if domain_match:
        domain = domain_match.group(1)
        url = f"https://{domain}"
        # If content/news keywords present, use scrape
        if _CONTENT_WORDS.search(text):
            calls.append(SkillCall(
                skill_name="scrape",
                arguments={"url": url, "max_results": "15"},
                raw_text=f"[auto-detected: scrape {url}]",
            ))
        else:
            calls.append(SkillCall(
                skill_name="fetch_url",
                arguments={"url": url, "max_chars": "3000"},
                raw_text=f"[auto-detected: fetch {url}]",
            ))
            calls.append(SkillCall(
                skill_name="web_search",
                arguments={"query": domain},
                raw_text=f"[auto-detected: web search for '{domain}']",
            ))
        return calls

    # Check for price/buy comparison queries that should always trigger web_search
    # e.g., "cuánto cuesta X", "donde comprar X más barato", "precio de X en Chile"
    if not calls:
        for price_pat in _PRICE_PATTERNS:
            if price_pat.search(text):
                query = _extract_search_query(text)
                if not query or len(query) < 3:
                    # Use the whole message as query (clean it up)
                    query = text.strip().rstrip("?!.")
                calls.append(SkillCall(
                    skill_name="web_search",
                    arguments={"query": query, "max_results": "10"},
                    raw_text=f"[auto-detected: price/buy web search for '{query}']",
                ))
                return calls

    # Check for web search patterns (only if no URL/domain and no datetime/weather)
    if not calls:
        query = _extract_search_query(text)
        if query:
            calls.append(SkillCall(
                skill_name="web_search",
                arguments={"query": query},
                raw_text=f"[auto-detected: web search for '{query}']",
            ))

    return calls
