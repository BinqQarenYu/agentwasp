"""Decision Layer — classifies user requests into execution strategies.

Determines the optimal execution path BEFORE sending to planner or LLM.
Uses pure heuristics (no LLM calls) — fast and deterministic.

Execution strategies:
  DIRECT_RESPONSE  → answer with LLM immediately, no persistence
  GOAL             → multi-step task, requires planning and tools
  SCHEDULED_TASK   → recurring execution at intervals
  SUB_AGENT        → long-term monitoring, domain-specialized agent
  SCRIPT           → deterministic code pipeline, minimal LLM reasoning

Safety rules:
  1. On any exception → return DIRECT_RESPONSE (let normal flow handle it)
  2. Never block execution — always return a strategy
  3. Never modify the request text
"""

from __future__ import annotations

import re
from enum import Enum

# Interval phrases → canonical human-readable string for task_manager
_INTERVAL_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(?:cada\s+30\s+minutos?|every\s+30\s+min(?:utes?)?)\b", re.IGNORECASE), "cada 30 minutos"),
    (re.compile(r"\b(?:cada\s+(\d+)\s+minutos?|every\s+(\d+)\s+min(?:utes?)?)\b", re.IGNORECASE), "cada {n} minutos"),
    (re.compile(r"\b(?:cada\s+(\d+)\s+horas?|every\s+(\d+)\s+hours?)\b", re.IGNORECASE), "cada {n}h"),
    (re.compile(r"\b(?:cada\s+hora|hourly)\b", re.IGNORECASE), "cada hora"),
    (re.compile(r"\b(?:cada\s+2\s+horas?|every\s+2\s+hours?)\b", re.IGNORECASE), "cada 2h"),
    (re.compile(r"\b(?:cada\s+6\s+horas?|every\s+6\s+hours?)\b", re.IGNORECASE), "cada 6h"),
    (re.compile(r"\b(?:cada\s+12\s+horas?|every\s+12\s+hours?)\b", re.IGNORECASE), "cada 12h"),
    (re.compile(r"\b(?:diariamente|diario|daily|cada\s+d[ií]a|every\s+day)\b", re.IGNORECASE), "diario"),
    (re.compile(r"\b(?:semanalmente|semanal|weekly|cada\s+semana|every\s+week)\b", re.IGNORECASE), "semanal"),
]


class Strategy(str, Enum):
    DIRECT_RESPONSE = "direct_response"
    GOAL = "goal"
    SCHEDULED_TASK = "scheduled_task"
    SUB_AGENT = "sub_agent"
    SCRIPT = "script"


# ---------------------------------------------------------------------------
# Compiled patterns (module-level — compiled once, reused)
# ---------------------------------------------------------------------------

# SCHEDULED_TASK — explicit recurring / interval markers
_SCHEDULE_PATTERNS = [
    re.compile(
        r"\b(?:cada|every)\s+(?:\d+\s+)?(?:hora[s]?|minuto[s]?|d[ií]a[s]?|semana[s]?|hour[s]?|minute[s]?|day[s]?|week[s]?)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:cada\s+hora|hourly|diariamente|daily|semanal(?:mente)?|weekly)\b", re.IGNORECASE),
    re.compile(r"\b(?:autom[aá]ticamente\s+cada|automatically\s+every|repeat(?:edly)?|de\s+forma\s+peri[oó]dica)\b", re.IGNORECASE),
    re.compile(r"\b(?:env[ií]a(?:me)?|send(?:\s+me)?|notif[ií]ca(?:me)?|alerta(?:me)?|alert\s+me)\b.{0,60}?\b(?:cada|every|hourly|daily)\b", re.IGNORECASE),
    re.compile(r"\b(?:programa(?:r)?|schedule|agendar?)\b.{0,40}?\b(?:tarea|task|reporte?|report|alerta?|alert|notificaci[oó]n)\b", re.IGNORECASE),
    re.compile(r"\b(?:tarea\s+programada|scheduled\s+task|cron\s+job)\b", re.IGNORECASE),
    # "monitorea/vigila/rastrea/automatiza" + asset/price subject → implied recurring
    re.compile(
        r"\b(?:monitor(?:ea|ear)?|vigil[ae](?:r)?|rastrea(?:r)?|automatiza(?:r)?|segu(?:ir|imiento))\b"
        r".{0,50}?\b(?:btc|bitcoin|eth|ethereum|cripto|crypto|precio|price|mercado|market|bolsa|asset|divisa|moneda|token)\b",
        re.IGNORECASE,
    ),
    # "automatiza" as standalone command (strong scheduling signal)
    re.compile(r"^(?:automatiza|automatizar|programa|programar)\b", re.IGNORECASE | re.MULTILINE),
    # "ejecutar cada" — explicit execution interval
    re.compile(r"\b(?:ejecutar?|run|execute)\b.{0,20}?\b(?:cada|every)\b", re.IGNORECASE),
    # "mantener/seguir monitoreando" — continuous monitoring intent
    re.compile(r"\b(?:mantener?|seguir?|keep)\b.{0,20}?\b(?:monitor(?:eando|izing)?|vigil(?:ando|ante)|rastreando|tracking)\b", re.IGNORECASE),
]

# SUB_AGENT — long-term monitoring or explicit agent creation
_SUB_AGENT_PATTERNS = [
    re.compile(r"\b(?:crea(?:r)?\s+(?:un\s+)?agente|create\s+(?:an?\s+)?agent|new\s+agent)\b", re.IGNORECASE),
    re.compile(r"\b(?:agente\s+(?:especializado|dedicado|aut[oó]nomo)|specialized\s+agent|dedicated\s+agent)\b", re.IGNORECASE),
    # "monitor ... continuously" — allow words between verb and adverb
    re.compile(r"\b(?:monitorea(?:r)?|monitor)\b.{0,40}?\bcontinuamente?\b", re.IGNORECASE),
    re.compile(r"\bmonitor\b.{0,40}?\bcontinuously\b", re.IGNORECASE),
    re.compile(r"\b(?:vigil[ae](?:r)?)\b.{0,40}?\bcontinuamente?\b", re.IGNORECASE),
    re.compile(r"\b(?:seguimiento\s+continuo|continuous\s+(?:monitoring|tracking)|track\s+continuously)\b", re.IGNORECASE),
    re.compile(r"\b(?:agente\s+de\s+(?:monitoreo|seguimiento|vigilancia|cripto|noticias|mercado))\b", re.IGNORECASE),
    re.compile(r"\b(?:sub[_\-\s]?agente|subagent|agent\s+worker)\b", re.IGNORECASE),
]

# GOAL — explicit multi-step complex task patterns
_GOAL_PATTERNS = [
    # Chained actions: "fetch/get X and/y analyze/send Y" (Spanish or English connectors)
    re.compile(
        r"\b(?:obtén?|fetch|obt[eé]n|busca[r]?|get|find)\b.{5,80}?(?:\by\b|\band\b).{0,20}?\b(?:analiza(?:r)?|genera(?:r)?|env[ií]a(?:me)?|guarda(?:r)?|procesa(?:r)?|send|generate|analyze|save|process)\b",
        re.IGNORECASE,
    ),
    # "analyze/investigate X and/y send/generate" — English "and" or Spanish "y"
    re.compile(
        r"\b(?:analiz[ae](?:r)?|analyze|investiga(?:r)?|research)\b.{5,80}?(?:\by\b|\band\b).{0,20}?\b(?:env[ií]a(?:me)?|genera(?:r)?|escribe|send|generate|write|report|reporte|summary|resumen)\b",
        re.IGNORECASE,
    ),
    # Explicit report/analysis generation
    re.compile(r"\b(?:genera(?:r)?\s+(?:un\s+)?(?:reporte?|informe|an[aá]lisis|report|analysis))\b", re.IGNORECASE),
    re.compile(r"\b(?:realiza(?:r)?\s+(?:un\s+)?an[aá]lisis\s+completo|perform\s+(?:a\s+)?(?:full|complete)\s+analysis)\b", re.IGNORECASE),
    # Research + deliver: "investiga X y envíame/send"
    re.compile(
        r"\b(?:investiga(?:r)?|research)\b.{5,80}?(?:\by\b|\band\b).{0,20}?\b(?:env[ií]a(?:me)?|send|summarize|resume(?:me)?|report)\b",
        re.IGNORECASE,
    ),
    # Multi-asset (BTC/ETH) + report/analysis
    re.compile(
        r"\b(?:btc|bitcoin|eth|ethereum|crypto|cripto)\b.{3,80}?\b(?:and\b|y\b).{3,80}?\b(?:reporte?|report|an[aá]lisis|analysis|send|env[ií]a)\b",
        re.IGNORECASE,
    ),
    # "X and Y and send/generate" chained action
    re.compile(
        r"\b\w+\b\s+and\s+\b\w+\b\s+and\s+(?:send|generate|analyze|build|create)\b",
        re.IGNORECASE,
    ),
    # Explicit goal/objective framing
    re.compile(r"^(?:objetivo|goal|tarea|task)\s*:", re.IGNORECASE | re.MULTILINE),
]

# SCRIPT — deterministic pipeline indicators
_SCRIPT_PATTERNS = [
    # "poll X API" or "poll the API" — allow words between poll and api
    re.compile(r"\bpoll(?:ing)?\b.{0,30}?\b(?:api|endpoint|url|binance|coinbase|coingecko)\b", re.IGNORECASE),
    re.compile(r"\b(?:extrae(?:r)?\s+datos|extract\s+data|data\s+extraction)\b", re.IGNORECASE),
    re.compile(r"\b(?:procesa(?:r)?\s+y\s+(?:genera|guarda|send)|process\s+and\s+(?:generate|save|send))\b", re.IGNORECASE),
    re.compile(r"\b(?:escri(?:be)?(?:r)?\s+(?:un\s+)?(?:script|c[oó]digo)|write\s+(?:a\s+)?(?:script|code))\b", re.IGNORECASE),
    re.compile(r"\b(?:pipeline|automatiza(?:r)?\s+el\s+proceso|automate\s+the\s+process)\b", re.IGNORECASE),
    re.compile(r"\b(?:web\s+scrap(?:ing|e)|scraper|crawler)\b", re.IGNORECASE),
    re.compile(r"\b(?:parse(?:ar)?|parsea(?:r)?) \b.{0,40}?\b(?:json|xml|csv|html|api)\b", re.IGNORECASE),
]

# DIRECT_RESPONSE — simple questions, factual queries, short interactions
_DIRECT_PATTERNS = [
    re.compile(r"^[¿]?\s*(?:qu[eé]\s+es|what\s+is|who\s+is|qui[eé]n\s+es|c[oó]mo\s+funciona|how\s+does)\b", re.IGNORECASE),
    re.compile(r"^[¿]?\s*(?:qu[eé]\s+significa|what\s+does\s+.+\s+mean|defin[ei](?:ci[oó]n)?)\b", re.IGNORECASE),
    re.compile(r"^[¿]?\s*(?:cu[aá]nto\s+(?:cuesta|vale|es)|how\s+much\s+(?:is|does)\s+.+\s+cost)\b", re.IGNORECASE),
    re.compile(r"^[¿]?\s*(?:cu[aá]ndo\s+(?:es|fue)|when\s+(?:is|was))\b", re.IGNORECASE),
    re.compile(r"\b(?:cu[eé]ntame\s+(?:sobre|acerca)|tell\s+me\s+about)\b", re.IGNORECASE),
    re.compile(r"\b(?:hola|hello|hi|buenos\s+d[ií]as|buenas\s+(?:tardes|noches))\b", re.IGNORECASE),
    re.compile(r"^[¿]?\s*(?:c[oó]mo\s+est[aá]s|how\s+are\s+you)\b", re.IGNORECASE),
]

# Exclusion patterns — never apply Decision Layer to these
_BYPASS_PATTERNS = [
    re.compile(r"^\[TAREA PROGRAMADA:", re.IGNORECASE),  # scheduler triggers
    re.compile(r"^\[AUTO-DETECTED:", re.IGNORECASE),      # internal auto-detects
]

# Retry / confirmation messages — always DIRECT_RESPONSE, never GOAL/AGENT/SCRIPT
# These are conversational turns that mean "yes, do it again" — not new tasks.
_RETRY_RE = re.compile(
    r"^(?:s[ií]|yes|ok|dale|bueno|claro|listo|venga)\s*[,.]?\s*(?:int[eé]ntalo\s+(?:de\s+nuevo|otra\s+vez|again)?|try\s+again|de\s+nuevo|otra\s+vez|retry|again)?$",
    re.IGNORECASE,
)

# Multi-step connectors that indicate GOAL complexity
_MULTI_STEP_RE = re.compile(
    r"\b(?:despu[eé]s\s+de|then|luego\s+de|a\s+continuaci[oó]n|after\s+that|once\s+done)\b",
    re.IGNORECASE,
)

# Words that hint at "please repeat/monitor" — boosts SCHEDULED_TASK confidence
_REPEAT_HINT_RE = re.compile(
    r"\b(?:mantener(?:me)?\s+actualizado|keep\s+(?:me\s+)?updated|avisar(?:me)?|notify\s+me|alert\s+me)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Semantic scheduling signals — 3-signal heuristic
# Signal A: continuous / recurring action verb
# Signal B: target entity (crypto, prices, URLs, named assets)
# A+B together → SCHEDULED_TASK even without explicit interval
# ---------------------------------------------------------------------------

_SEM_ACTION_RE = re.compile(
    r"\b(?:"
    # Spanish
    r"monitor(?:ea|ear|eo)?|vigil[ae](?:r)?|rastrea(?:r)?|observa(?:r)?|"
    r"sigue?|seguir|chequea(?:r)?|controla(?:r)?|supervisar?|supervisa(?:r)?|"
    r"actualiza(?:me)?\s+(?:sobre|con)|mant[eé]n(?:me)?\s+informad[oa]|"
    r"informar?(?:me)?\s+(?:sobre|de)|avis[ae](?:me)?\s+(?:si|cuando|sobre)|"
    r"est[aé]\s+pendiente|presta\s+atenci[oó]n|"
    # English
    r"track(?:ing)?|watch(?:ing)?|follow(?:ing)?|observe(?:s|ing)?|"
    r"monitor(?:ing)?|keep\s+(?:an?\s+)?eye\s+on|check(?:ing)?\b|"
    r"stay\s+(?:on\s+top\s+of|updated\s+on|informed\s+about|alert(?:ed)?\s+(?:for|about))|"
    r"keep\s+(?:me\s+)?(?:posted|updated|informed|notified)\s+(?:on|about)"
    r")\b",
    re.IGNORECASE,
)

_SEM_ENTITY_RE = re.compile(
    r"\b(?:"
    r"btc|bitcoin|eth|ethereum|sol|solana|bnb|xrp|ripple|ada|cardano|"
    r"doge|dogecoin|ltc|litecoin|matic|polygon|avax|avalanche|dot|polkadot|"
    r"link|chainlink|cripto|crypto|precio|price|mercado|market|bolsa|"
    r"exchange|token|coin|divisa|moneda|activo|asset|eur|usd|dollar|euro"
    r")\b|https?://\S+",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def decide_execution_strategy(text: str) -> Strategy:
    """Classify the execution strategy for a user request.

    Pure heuristic classification — no LLM calls, deterministic.
    Returns DIRECT_RESPONSE for simple queries, GOAL for complex tasks,
    SCHEDULED_TASK for recurring requests, SUB_AGENT for long-term monitoring,
    SCRIPT for deterministic pipelines.

    For long inputs (>400 chars), uses a compact summary for routing to prevent
    misclassification from noise in lengthy instructions.

    Falls back to DIRECT_RESPONSE on any exception.
    """
    try:
        stripped = text.strip()
        routing_text = _compact_for_routing(stripped)
        return _classify(routing_text)
    except Exception:
        return Strategy.DIRECT_RESPONSE


def _compact_for_routing(text: str, max_len: int = 400) -> str:
    """Return a compact version of the text suitable for intent routing.

    For short texts, returns as-is. For long texts, extracts signals from the
    ENTIRE text (not just the beginning) so critical routing info is never lost:
    - First sentence / first line (captures primary intent)
    - ALL scheduling phrases ("cada X horas", "regularmente", etc.)
    - ALL asset/entity mentions (BTC, ETH, tickers)
    - ALL URLs (critical for monitoring tasks)
    """
    if len(text) <= max_len:
        return text

    # First sentence (up to first period/newline/semicolon, max 200 chars)
    first_sentence = re.split(r"[.\n;]", text[:250])[0].strip()

    # Extract ALL scheduling phrases from full text
    _sched_re = re.compile(
        r"\b(?:cada\s+(?:\d+\s+)?(?:hora[s]?|minuto[s]?|d[ií]a[s]?|semana[s]?)|"
        r"cada\s+hora|diariamente|hourly|daily|semanal(?:mente)?|weekly|"
        r"autom[aá]ticamente\s+cada|automatically\s+every|"
        r"regularmente|peri[oó]dicamente|frecuentemente|regularly|periodically)\b",
        re.IGNORECASE,
    )
    sched_phrases = list(dict.fromkeys(_sched_re.findall(text)))

    # Extract ALL entity mentions from full text (crypto tickers, price terms)
    _asset_re = re.compile(
        r"\b(?:btc|bitcoin|eth|ethereum|sol|solana|bnb|xrp|ada|cardano|"
        r"doge|dogecoin|matic|avax|dot|link|cripto|crypto|precio|price|"
        r"mercado|market|token|coin|divisa|moneda|activo|asset)\b",
        re.IGNORECASE,
    )
    assets = list(dict.fromkeys(_asset_re.findall(text)))

    # Extract ALL URLs from full text (never drop a URL — it's often the target)
    _url_re = re.compile(r"https?://\S+")
    urls = _url_re.findall(text)

    parts = [first_sentence]
    if sched_phrases:
        parts.append(" ".join(sched_phrases[:3]))
    if assets:
        parts.append(" ".join(assets[:5]))
    if urls:
        parts.append(" ".join(urls[:2]))

    compact = " ".join(parts)
    return compact[:max_len]


def get_routing_hint(strategy: Strategy) -> str:
    """Return a short routing hint string to inject into LLM context.

    Used when the LLM needs to handle the request but benefits from
    knowing the intended execution strategy.
    """
    _hints = {
        Strategy.SCHEDULED_TASK: "[ROUTING: use task_manager(action='create') to schedule this as a recurring task]",
        Strategy.SUB_AGENT: "[ROUTING: use agent_manager(action='create') to create a specialized agent for this]",
        Strategy.SCRIPT: "[ROUTING: use python_exec() or shell() for deterministic execution instead of manual steps]",
        Strategy.GOAL: "",        # GOAL routes directly to GoalOrchestrator, no hint needed
        Strategy.DIRECT_RESPONSE: "",  # No hint needed
    }
    return _hints.get(strategy, "")


# ---------------------------------------------------------------------------
# Internal classification logic
# ---------------------------------------------------------------------------


def _classify(text: str) -> Strategy:
    """Core classification — called from decide_execution_strategy with stripped text."""

    # Never apply Decision Layer to internal scheduler triggers or auto-detects
    for bypass in _BYPASS_PATTERNS:
        if bypass.search(text):
            return Strategy.DIRECT_RESPONSE

    # Very short messages (< 7 chars) are almost always conversational (hi, ok, sí, no, etc.)
    # Don't block short-but-valid commands like "track BTC" (9 chars) or "watch SOL"
    if len(text) < 7:
        return Strategy.DIRECT_RESPONSE

    # Retry/confirmation messages — "sí inténtalo de nuevo", "ok try again", etc.
    # These are conversational turns, never new goals or tasks.
    if _RETRY_RE.match(text.strip()):
        return Strategy.DIRECT_RESPONSE

    # --- Priority 1: SCHEDULED_TASK --- (highest confidence first)
    _is_question = bool(re.search(r"^[¿]|[?]$|\b(?:qu[eé]|what|cu[aá]ndo|when|c[oó]mo|how)\b.*\?", text, re.IGNORECASE))

    schedule_hits = sum(1 for p in _SCHEDULE_PATTERNS if p.search(text))
    if schedule_hits >= 1 and not _is_question:
        return Strategy.SCHEDULED_TASK

    # Semantic heuristic: continuous action verb + target entity → scheduling intent
    # even without explicit "cada X horas" (e.g. "track BTC", "follow ETH price")
    if not _is_question:
        if _SEM_ACTION_RE.search(text) and _SEM_ENTITY_RE.search(text):
            return Strategy.SCHEDULED_TASK

    # Also trigger on "keep me updated" + frequency hint
    if _REPEAT_HINT_RE.search(text) and schedule_hits >= 1:
        return Strategy.SCHEDULED_TASK

    # --- Priority 2: SUB_AGENT ---
    agent_hits = sum(1 for p in _SUB_AGENT_PATTERNS if p.search(text))
    if agent_hits >= 1:
        return Strategy.SUB_AGENT

    # --- Priority 3: SCRIPT ---
    script_hits = sum(1 for p in _SCRIPT_PATTERNS if p.search(text))
    if script_hits >= 1:
        return Strategy.SCRIPT

    # --- Priority 4: GOAL (multi-step) ---
    goal_hits = sum(1 for p in _GOAL_PATTERNS if p.search(text))
    multi_step = bool(_MULTI_STEP_RE.search(text))

    # Strong GOAL signal: 2+ goal pattern hits
    if goal_hits >= 2:
        return Strategy.GOAL
    # Moderate GOAL signal: 1 goal hit + multi-step connector
    if goal_hits >= 1 and multi_step:
        return Strategy.GOAL
    # Any goal hit (chained action pattern already implies multi-step)
    if goal_hits >= 1:
        return Strategy.GOAL

    # --- Priority 5: DIRECT_RESPONSE (simple questions) ---
    direct_hits = sum(1 for p in _DIRECT_PATTERNS if p.search(text))
    if direct_hits >= 1:
        return Strategy.DIRECT_RESPONSE

    # --- Default: DIRECT_RESPONSE ---
    # Let the existing LLM + planning escalation handle ambiguous cases naturally
    return Strategy.DIRECT_RESPONSE


# ---------------------------------------------------------------------------
# Parameter extraction helpers (for direct routing)
# ---------------------------------------------------------------------------


def generate_task_title(text: str, interval: str = "") -> str:
    """Generate a short, human-readable task title from a user request.

    Examples:
      "Quiero que cada 2 minutos revises el precio de BTC y me envíes un informe"
      → "Informe de BTC cada 2 minutos"

      "Cada día a las 9 AM envíame un resumen de ETH por correo"
      → "Informe diario de ETH"

    Rules:
    - 4–8 words max
    - No underscores, no raw user input formatting
    - Extracts: action type + topic/asset + frequency
    """
    txt_lower = text.lower()

    # ── 1. Detect action type ────────────────────────────────────────────────
    action = "Monitoreo"
    if re.search(r"\b(informe|reporte|report|resumen|summary|envía|enviar|manda|send|correo|email)\b", txt_lower):
        action = "Informe"
    elif re.search(r"\b(alerta|alert|avisa|avísame|notify|notifica|si\s+(?:sube|baja|supera|cae))\b", txt_lower):
        action = "Alerta"
    elif re.search(r"\b(revisa|revisar|check|verifica|analiza|analyze)\b", txt_lower):
        action = "Revisión"
    elif re.search(r"\b(captura|screenshot|foto|imagen|image)\b", txt_lower):
        action = "Captura"
    elif re.search(r"\b(precio|price|cotiza|cotización)\b", txt_lower):
        action = "Precio"

    # ── 2. Detect main topic/asset ───────────────────────────────────────────
    topic = ""
    # Multi-asset check first: "BTC y ETH", "bitcoin y ethereum"
    _multi = re.findall(r"\b(btc|eth|sol|bnb|ada|xrp|doge|bitcoin|ethereum|solana|dogecoin)\b", txt_lower)
    _unique = list(dict.fromkeys(  # preserve order, deduplicate
        "BTC" if t in ("btc", "bitcoin") else
        "ETH" if t in ("eth", "ethereum") else
        "SOL" if t in ("sol", "solana") else
        "DOGE" if t in ("doge", "dogecoin") else
        t.upper()
        for t in _multi
    ))
    if len(_unique) >= 2:
        topic = " y ".join(_unique[:2])
    else:
        # Single crypto asset
        _CRYPTO = [
            ("BTC", r"\b(btc|bitcoin)\b"),
            ("ETH", r"\b(eth|ethereum)\b"),
            ("SOL", r"\b(sol|solana)\b"),
            ("BNB", r"\b(bnb)\b"),
            ("ADA", r"\b(ada|cardano)\b"),
            ("XRP", r"\b(xrp|ripple)\b"),
            ("DOGE", r"\b(doge|dogecoin)\b"),
            ("crypto", r"\b(crypto|cripto|criptomonedas?)\b"),
        ]
        for label, pat in _CRYPTO:
            if re.search(pat, txt_lower):
                topic = label
                break

    # Other topics
    if not topic:
        if re.search(r"\b(clima|weather|temperatura|lluvia|forecast|pron[oó]stico)\b", txt_lower):
            topic = "clima"
        elif re.search(r"\b(noticias?|news|titulares?|headlines?)\b", txt_lower):
            topic = "noticias"
        elif re.search(r"\b(bolsa|acciones?|stocks?|s&p|nasdaq|nyse)\b", txt_lower):
            topic = "mercados"
        elif re.search(r"\b(gas|combustible|gasolina|fuel)\b", txt_lower):
            topic = "combustible"
        elif re.search(r"\b(d[oó]lar|dollar|usd|eur|euro)\b", txt_lower):
            topic = "divisas"

    # ── 3. Format interval compactly ────────────────────────────────────────
    interval_short = ""
    if interval:
        iv = interval.lower().strip()
        if iv in ("diario", "cada día", "daily"):
            interval_short = "diario"
        elif iv in ("semanal", "cada semana", "weekly"):
            interval_short = "semanal"
        elif iv in ("cada hora", "hourly"):
            interval_short = "cada hora"
        elif re.match(r"cada (\d+)h", iv):
            m = re.match(r"cada (\d+)h", iv)
            interval_short = f"cada {m.group(1)}h"
        elif re.match(r"cada (\d+) minutos?", iv):
            m = re.match(r"cada (\d+) minutos?", iv)
            interval_short = f"cada {m.group(1)} min"
        elif re.match(r"cada (\d+) horas?", iv):
            m = re.match(r"cada (\d+) horas?", iv)
            interval_short = f"cada {m.group(1)}h"

    # ── 4. Assemble title ────────────────────────────────────────────────────
    parts = [action]
    if topic:
        parts.append("de")
        parts.append(topic)
    if interval_short and interval_short not in ("diario", "semanal"):
        parts.append(interval_short)
    elif interval_short in ("diario", "semanal"):
        # "Informe diario de BTC" — put frequency before topic
        parts = [action, interval_short]
        if topic:
            parts += ["de", topic]

    title = " ".join(parts)
    # Cap at 50 chars, never empty
    return title[:50] if title else "Tarea programada"


def extract_task_params(text: str) -> dict:
    """Extract task_manager parameters from a SCHEDULED_TASK request.

    Returns dict with: name (str), instruction (str), interval (str).
    Falls back to safe defaults if extraction is ambiguous.
    """
    interval = "cada hora"  # safe default
    for pattern, template in _INTERVAL_MAP:
        m = pattern.search(text)
        if m:
            if "{n}" in template:
                # Capture the number from whichever group matched
                n = next((g for g in m.groups() if g is not None), "1")
                interval = template.replace("{n}", n)
            else:
                interval = template
            break

    name = generate_task_title(text, interval)
    return {"name": name, "instruction": text, "interval": interval}


def extract_agent_params(text: str) -> dict:
    """Extract agent_manager parameters from a SUB_AGENT request.

    Returns dict with: name (str), description (str), autonomy_mode (str).
    """
    # Try to extract an explicit agent name after "agente de X" or "agent for X"
    name_match = re.search(
        r"\b(?:agente\s+de\s+|agent\s+for\s+|agente\s+(?:especializado\s+en|dedicado\s+a)\s+)(\w+(?:\s+\w+){0,2})",
        text,
        re.IGNORECASE,
    )
    if name_match:
        raw = name_match.group(1).strip()
        name = re.sub(r"\s+", "_", raw).title() + "Agent"
    else:
        # Fallback: first noun-like words
        words = re.sub(r"[^\w\s]", "", text).split()
        noun_words = [w for w in words if len(w) > 3 and w.lower() not in
                      {"crea", "crear", "create", "agente", "agent", "nuevo", "new",
                       "para", "that", "with", "monitor", "monitorea", "monitorear"}][:3]
        name = "".join(w.capitalize() for w in noun_words) + "Agent" if noun_words else "MonitorAgent"

    name = name[:50]
    description = text[:200]
    return {"name": name, "description": description, "autonomy_mode": "semi"}


# ---------------------------------------------------------------------------
# Introspection helpers (for logging / debugging)
# ---------------------------------------------------------------------------


def is_scheduling_request(text: str) -> bool:
    """Fast check: does this text look like a scheduling/monitoring request?

    Used to bypass the Capability Engine for messages that should create tasks/agents.
    Does NOT require a full classification — just checks for strong scheduling signals.
    """
    try:
        compact = _compact_for_routing(text.strip())
        hits = sum(1 for p in _SCHEDULE_PATTERNS if p.search(compact))
        return hits >= 1
    except Exception:
        return False


def explain_strategy(text: str) -> dict:
    """Return a debug dict explaining why a strategy was chosen."""
    text = text.strip()
    return {
        "strategy": decide_execution_strategy(text).value,
        "schedule_hits": sum(1 for p in _SCHEDULE_PATTERNS if p.search(text)),
        "agent_hits": sum(1 for p in _SUB_AGENT_PATTERNS if p.search(text)),
        "script_hits": sum(1 for p in _SCRIPT_PATTERNS if p.search(text)),
        "goal_hits": sum(1 for p in _GOAL_PATTERNS if p.search(text)),
        "direct_hits": sum(1 for p in _DIRECT_PATTERNS if p.search(text)),
        "multi_step": bool(_MULTI_STEP_RE.search(text)),
        "text_len": len(text),
    }
