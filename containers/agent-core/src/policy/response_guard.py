"""Final-response policy.

Every user-facing response, regardless of path (Telegram normal/fast-path,
dashboard chat_direct, scheduled-task triggers, error fallback), goes
through `apply_final_response_policy()` exactly once.

Order of enforcement:
  1. enforce_schedule_honesty   — strip clock-time lies
  2. enforce_side_effect_text_gate — strip unauthorized "I will email/schedule"
  3. (caller may chain prompt-leak strip / markdown clean — those are
      Telegram-specific and stay in handlers.py because they depend on
      transport)

The policy is pure: same inputs → same outputs, no Redis, no DB, no clock.
A trace dict is returned describing what changed; callers can attach it to
the per-request DecisionTrace.
"""
from __future__ import annotations

import re
from typing import Any, Optional

# ── Schedule honesty ─────────────────────────────────────────────────────

TIME_CLAIM_RE = re.compile(
    r"\b("
    r"a\s+las\s+\d{1,2}(?::\d{2})?(?:\s*(?:am|pm|a\.m\.|p\.m\.|hrs?|h|de\s+la\s+(?:ma[ñn]ana|tarde|noche)))?"
    r"|at\s+\d{1,2}(?::\d{2})?(?:\s*(?:am|pm|a\.m\.|p\.m\.|o['’]clock))?"
    r"|\d{1,2}:\d{2}\s*(?:am|pm|a\.m\.|p\.m\.|hrs?|h)"
    r"|\d{1,2}\s*(?:am|pm|a\.m\.|p\.m\.)"
    r")\b",
    re.IGNORECASE,
)

# Daypart phrases imply a fixed-time request without naming a clock value.
# task_manager only supports interval scheduling, so these need the same
# disclaimer as a clock-time request. Captured via a separate regex so the
# disclaimer phrasing can be tailored ("no se ejecuta por la mañana
# específicamente" vs "no se ejecuta a las 9am específicamente").
DAYPART_CLAIM_RE = re.compile(
    r"\b("
    r"por\s+la\s+(?:ma[ñn]ana|tarde|noche)"
    r"|en\s+la\s+(?:ma[ñn]ana|tarde|noche)"
    r"|in\s+the\s+(?:morning|afternoon|evening|night)"
    r"|every\s+(?:morning|afternoon|evening|night)"
    r"|cada\s+(?:ma[ñn]ana|tarde|noche)"
    r"|al\s+amanecer|al\s+atardecer|al\s+anochecer"
    r"|at\s+(?:dawn|sunrise|dusk|sunset|noon|midnight)"
    r")\b",
    re.IGNORECASE,
)

SCHEDULING_CONTEXT_RE = re.compile(
    r"\b("
    r"tarea|programad[oa]|programar|programada|"
    r"task|scheduled|schedule|recurring|"
    r"diari[oa]|daily|todos\s+los\s+d[ií]as|every\s+day|cada\s+d[ií]a|"
    r"resumen|informe|reporte|report|"
    r"recordatorio|reminder|aviso|notify"
    r")\b",
    re.IGNORECASE,
)


def extract_fixed_time_unhonored(skill_results) -> str:
    """Return FIXED_TIME_REQUESTED string from a task_manager.create result
    whose output marks the time as NOT HONORED. Empty if none."""
    if not skill_results:
        return ""
    for r in skill_results:
        try:
            if getattr(r, "skill_name", "") != "task_manager":
                continue
            out = getattr(r, "output", "") or ""
            if "FIXED_TIME_REQUESTED:" not in out or "NOT HONORED" not in out:
                continue
            m = re.search(r"FIXED_TIME_REQUESTED:\s*([^\n—]+)\s*—", out)
            if m:
                return m.group(1).strip()
        except Exception:
            continue
    return ""


def has_real_task_create(skill_results) -> bool:
    """True iff any task_manager skill result this turn represents a real
    task creation (or dedup hit). Used by schedule-honesty guard to choose
    between "task created but time not honored" vs "no task created"."""
    if not skill_results:
        return False
    for r in skill_results:
        try:
            if getattr(r, "skill_name", "") != "task_manager":
                continue
            if not getattr(r, "success", False):
                continue
            out = (getattr(r, "output", "") or "").strip()
            if out.startswith("Task created:") or "already exists" in out:
                return True
            if "Not creating a duplicate" in out or "A task for this agent already exists" in out:
                return True
        except Exception:
            continue
    return False


def task_has_at_time_honored(skill_results) -> bool:
    """Phase 4/8: True iff any task_manager success result this turn shows
    that the requested clock time WAS actually persisted (at_time field
    populated) and the next_run is at the user's clock time. When True, the
    schedule-honesty disclaimer must NOT be appended — the task IS scheduled
    at the user's chosen time and saying otherwise is a capability lie.
    """
    if not skill_results:
        return False
    for r in skill_results:
        try:
            if getattr(r, "skill_name", "") != "task_manager":
                continue
            if not getattr(r, "success", False):
                continue
            out = (getattr(r, "output", "") or "")
            # Evidence patterns from the actual task_manager output:
            #   "at_time: 8:00 AM"     (Redis dump)
            #   "Next run: 2026-05-06 08:00 (hora Chile)"   (formatted)
            #   "Scheduled at 08:00"   (alternative)
            if re.search(r"at_time\s*[:=]\s*['\"]?\d{1,2}", out, re.IGNORECASE):
                return True
            if re.search(r"next\s*[_\s]?run\b[^.\n]{0,80}\d{1,2}:\d{2}\s*(?:hora|chile|local)?",
                         out, re.IGNORECASE):
                return True
            if re.search(r"\bscheduled\s+at\s+\d{1,2}:\d{2}\b", out, re.IGNORECASE):
                return True
        except Exception:
            continue
    return False


def enforce_schedule_honesty(
    response_text: str,
    skill_results,
    user_lang: str = "en",
    user_text: str = "",
) -> tuple[str, dict]:
    """Strip clock-time scheduling lies. Returns (cleaned_text, trace).

    trace = {"applied": bool, "claimed_time": str, "had_real_create": bool}

    Two-direction enforcement:
      1. Agent-side: response claims a clock time → strip it (existing).
      2. User-side: user asked for a clock time AND a task was actually
         created interval-only → append disclaimer that fixed-time isn't
         honored. Catches the silent-misinterpretation pattern where the
         agent creates "every hour" for "every Monday at 9am" without
         disclosing the schedule semantics.
    """
    trace = {"applied": False, "claimed_time": "", "had_real_create": False}
    if not response_text:
        return response_text, trace

    time_match = TIME_CLAIM_RE.search(response_text)
    # Phase 4/8: if the task ACTUALLY persisted at_time, the disclaimer is a
    # capability lie — task_manager DOES support clock-time scheduling now.
    # Skip the entire honesty enforcement when the time was honored.
    if task_has_at_time_honored(skill_results):
        trace["applied"] = False
        trace["skipped_reason"] = "at_time_honored"
        return response_text, trace

    if not time_match:
        # Agent didn't claim a clock time. Check if USER did and a task was
        # created — that's the silent-lie-by-omission case.
        if user_text and has_real_task_create(skill_results):
            user_time_match = TIME_CLAIM_RE.search(user_text)
            user_daypart_match = DAYPART_CLAIM_RE.search(user_text)
            if user_time_match:
                # Reject pure-numeric matches from the user side that are
                # likely intervals like "cada 2 horas" — only treat actual
                # AM/PM / clock-time strings as fixed-time markers.
                _candidate = user_time_match.group(1).strip()
                if re.search(r"(am|pm|a\.m\.|p\.m\.|:|de\s+la|o['’]clock)", _candidate, re.IGNORECASE):
                    requested = re.sub(
                        r"^(?:a\s+las\s+|at\s+)", "", _candidate, flags=re.IGNORECASE,
                    ).strip()
                    suffix_es = (
                        f"\n\nNota: la tarea no se ejecuta a las {requested} específicamente — "
                        f"task_manager solo soporta intervalos. Corre cada N horas desde el momento de creación."
                    )
                    suffix_en = (
                        f"\n\nNote: the task does not run at {requested} specifically — "
                        f"task_manager only supports interval scheduling. It runs every N hours from creation time."
                    )
                    cleaned = response_text.rstrip(" ,;.\n") + "."
                    cleaned = cleaned + (suffix_es if user_lang == "es" else suffix_en)
                    trace["applied"] = True
                    trace["claimed_time"] = requested
                    trace["had_real_create"] = True
                    trace["origin"] = "user_text"
                    return cleaned, trace
            elif user_daypart_match:
                # Natural-language daypart ("por la mañana", "in the
                # morning", etc.) — same disclaimer family.
                _daypart = user_daypart_match.group(1).strip()
                suffix_es = (
                    f"\n\nNota: la tarea no se ejecuta {_daypart} específicamente — "
                    f"task_manager solo soporta intervalos. Corre cada N horas desde el momento de creación."
                )
                suffix_en = (
                    f"\n\nNote: the task does not run {_daypart} specifically — "
                    f"task_manager only supports interval scheduling. It runs every N hours from creation time."
                )
                cleaned = response_text.rstrip(" ,;.\n") + "."
                cleaned = cleaned + (suffix_es if user_lang == "es" else suffix_en)
                trace["applied"] = True
                trace["claimed_time"] = _daypart
                trace["had_real_create"] = True
                trace["origin"] = "user_text_daypart"
                return cleaned, trace
        return response_text, trace
    if not SCHEDULING_CONTEXT_RE.search(response_text):
        return response_text, trace

    requested = time_match.group(1).strip()
    # Strip leading "a las " / "at " so the disclaimer reads naturally
    # ("no se ejecuta a las 8 am" instead of "a las a las 8 am").
    requested = re.sub(r"^(?:a\s+las\s+|at\s+)", "", requested, flags=re.IGNORECASE).strip()
    cleaned = TIME_CLAIM_RE.sub("", response_text)
    cleaned = re.sub(r"\s+,", ",", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).rstrip(" ,;.\n") + "."

    has_real = has_real_task_create(skill_results)
    if has_real:
        suffix_es = (
            f"\n\nNota: la tarea no se ejecuta a las {requested} específicamente — "
            f"task_manager solo soporta intervalos. Corre cada N horas desde el momento de creación."
        )
        suffix_en = (
            f"\n\nNote: the task does not run at {requested} specifically — "
            f"task_manager only supports interval scheduling. It runs every N hours from creation time."
        )
    else:
        suffix_es = (
            f"\n\nNota: no puedo programar a las {requested} — task_manager solo soporta intervalos. "
            f"Si quieres, dime un intervalo (cada Nh, diario, semanal) y la creo."
        )
        suffix_en = (
            f"\n\nNote: I cannot schedule at {requested} — task_manager only supports interval scheduling. "
            f"Tell me an interval (every Nh, daily, weekly) and I will create it."
        )
    cleaned = cleaned + (suffix_es if user_lang == "es" else suffix_en)

    trace["applied"] = True
    trace["claimed_time"] = requested
    trace["had_real_create"] = has_real
    return cleaned, trace


# ── Side-effect text gate ────────────────────────────────────────────────

SIDE_EFFECT_ANNOUNCEMENT_PATTERNS = {
    # ENV* / MAND* verb stems cover all common Spanish forms:
    # envío/envía/enviar/enviaré/envié/envió/mando/manda/mandar/mandaré, etc.
    # Followed by an optional object phrase, then "por correo/email/gmail".
    "email": re.compile(
        r"\b(?:"
        r"(?:te\s+)?(?:lo\s+)?"
        r"(?:env[íi][aáoeé]?(?:r|s|mos|ron|ré|rá|ría)?|"
        r"mand[aáoeéó]?(?:r|s|mos|ron|ré|rá|ría)?|"
        r"envio|envia|mando|manda)"
        r"\s+(?:lo\s+|la\s+|los\s+|las\s+|el\s+|"
        r"(?:el|un|este|ese)\s+(?:resumen|reporte|informe|correo|email|update))?\s*"
        r"(?:por\s+|via\s+|al\s+)?(?:correo|email|e-?mail|gmail|inbox)|"
        r"voy\s+a\s+(?:enviar|mandar)\s+(?:.+?)?(?:correo|email|gmail)|"
        r"procedo\s+a\s+(?:enviar|mandar)(?:lo|la)?|"
        r"I\s+(?:will|'ll)\s+(?:send|email)\s+(?:you\s+)?(?:the\s+)?(?:report|summary|email|update)|"
        r"now\s+I\s+(?:will|'ll)\s+(?:send|email)|"
        r"sending\s+(?:you|this)\s+(?:by|via)\s+email"
        r")\b",
        re.IGNORECASE,
    ),
    # Task creation announcements — past-tense, present-perfect, AND future
    # ("crearé la tarea ahora", "I will schedule it"). The latter is a lie
    # if no real task_manager.create call ran this turn.
    "task_create": re.compile(
        r"\b(?:"
        r"(?:he|ya)\s+(?:programad[oa]|program[éeo]|cre[éeo]|cread[oa])\s+"
        r"(?:una\s+|el\s+|la\s+)?(?:tarea|recordatorio\s+recurrente)|"
        r"(?:tarea|task|recordatorio\s+recurrente)\s+"
        r"(?:program(?:ada|ado|ed)|cread[ao]|scheduled|created)|"
        r"(?:I|i)\s+(?:have|'ve)?\s*(?:scheduled|created)\s+(?:a|the)\s+(?:task|recurring|reminder)|"
        r"qued[óo]\s+programad[oa]|"
        # Future-tense Spanish — "crearé/creo/programaré/voy a programar la tarea"
        r"(?:cre(?:ar[ée]|ar[áa]|o|ando)|program(?:ar[ée]|ar[áa]|o|ando))\s+"
        r"(?:la\s+|una\s+|el\s+)?(?:tarea|recordatorio)|"
        r"voy\s+a\s+(?:crear|programar)\s+(?:la|una|el)?\s*(?:tarea|recordatorio)|"
        # Future-tense English
        r"I'?ll\s+(?:schedule|create|set\s+up)\s+(?:a|the)\s+(?:task|recurring|reminder)"
        r")\b",
        re.IGNORECASE,
    ),
    "agent_create": re.compile(
        r"\b(?:"
        r"(?:he\s+)?cre(?:ado|é)\s+(?:un|el)\s+(?:sub-?)?agente|"
        r"agente\s+(?:cread[oa]|nuevo)\s+(?:exitosamente|correctamente)|"
        r"(?:I|i)\s+(?:have|'ve)?\s*created\s+(?:a|the|an)\s+(?:sub-?)?agent|"
        r"agent\s+(?:created|set\s+up)"
        r")\b",
        re.IGNORECASE,
    ),
}


def enforce_side_effect_text_gate(
    response_text: str,
    user_text: str,
    skill_results,
    chat_id: str = "",
    user_lang: str = "en",
    recent_action_resolver=None,
) -> tuple[str, dict]:
    """Scrub side-effect announcements that the user did not authorize.

    Returns (cleaned_text, trace).
    trace = {"rewrites": list[str]}.
    """
    from .intent_gate import (
        INTENT_GATE_PATTERNS as _IGP,
        REFERENCE_PHRASE_RE as _RPR,
    )

    trace = {"rewrites": []}
    if not response_text:
        return response_text, trace

    ran_skills: set[str] = set()
    for r in skill_results or []:
        try:
            sn = (getattr(r, "skill_name", "") or "").lower()
            if not getattr(r, "success", False):
                continue
            if sn == "gmail":
                ran_skills.add("gmail_send")
            elif sn == "task_manager":
                out = (getattr(r, "output", "") or "").strip()
                if out.startswith("Task created:") or "already exists" in out:
                    ran_skills.add("task_create")
            elif sn == "agent_manager":
                out = (getattr(r, "output", "") or "").strip()
                if "created successfully" in out or ("Agent " in out and "created" in out):
                    ran_skills.add("agent_create")
        except Exception:
            continue

    cleaned = response_text
    rewrites: list[str] = []

    # Phase 4/8: when the current turn was a task_create, the response
    # legitimately DESCRIBES what the scheduled task will do in the future
    # (e.g. "every day will email you the weather"). Don't strip those
    # forward-looking descriptions; they're not unauthorized side effects.
    _is_task_create_turn = "task_create" in ran_skills

    if SIDE_EFFECT_ANNOUNCEMENT_PATTERNS["email"].search(cleaned) and "gmail_send" not in ran_skills:
        explicit = bool(_IGP["gmail"].search(user_text or ""))
        if not explicit and not _is_task_create_turn:
            ctx_ok = False
            if _RPR.search(user_text or "") and recent_action_resolver:
                last_act = recent_action_resolver(chat_id, "gmail") or {}
                ctx_ok = bool(last_act and last_act.get("action") == "send")
            if not ctx_ok:
                cleaned = SIDE_EFFECT_ANNOUNCEMENT_PATTERNS["email"].sub("", cleaned)
                rewrites.append("email")

    if SIDE_EFFECT_ANNOUNCEMENT_PATTERNS["task_create"].search(cleaned) and "task_create" not in ran_skills:
        if not _IGP["task_manager"].search(user_text or ""):
            cleaned = SIDE_EFFECT_ANNOUNCEMENT_PATTERNS["task_create"].sub("", cleaned)
            rewrites.append("task_create")

    if SIDE_EFFECT_ANNOUNCEMENT_PATTERNS["agent_create"].search(cleaned) and "agent_create" not in ran_skills:
        if not _IGP["agent_manager"].search(user_text or ""):
            cleaned = SIDE_EFFECT_ANNOUNCEMENT_PATTERNS["agent_create"].sub("", cleaned)
            rewrites.append("agent_create")

    if rewrites:
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,;.\n")
        cleaned = (cleaned + ".") if cleaned else ("Listo." if user_lang == "es" else "Done.")
        trace["rewrites"] = rewrites
    return cleaned, trace


# ── Single entry point — every path goes through this ────────────────────


# ── Language consistency (deterministic, no LLM) ─────────────────────────
#
# Catches the case where a skill output (e.g. get_datetime returning
# "domingo, 29 de abril") leaks into a response delivered to an English
# user. The policy detects mismatched language at the END of the pipeline
# and either suppresses the leaked block or appends a one-line user-language
# wrapper.
#
# This is intentionally narrow — we DO NOT translate full responses. We
# only translate well-known short phrases (datetime weekday/month) using a
# tiny static lookup. For anything beyond that, the LLM is expected to wrap
# the data in the user's language (per prime.md §8). This is a safety net,
# not a translator.

_ES_WEEKDAYS = {
    "lunes": "Monday", "martes": "Tuesday", "miércoles": "Wednesday",
    "miercoles": "Wednesday", "jueves": "Thursday", "viernes": "Friday",
    "sábado": "Saturday", "sabado": "Saturday", "domingo": "Sunday",
}
_ES_MONTHS = {
    "enero": "January", "febrero": "February", "marzo": "March",
    "abril": "April", "mayo": "May", "junio": "June", "julio": "July",
    "agosto": "August", "septiembre": "September", "octubre": "October",
    "noviembre": "November", "diciembre": "December",
}
_EN_WEEKDAYS = {
    "monday": "lunes", "tuesday": "martes", "wednesday": "miércoles",
    "thursday": "jueves", "friday": "viernes",
    "saturday": "sábado", "sunday": "domingo",
}
_EN_MONTHS = {
    "january": "enero", "february": "febrero", "march": "marzo",
    "april": "abril", "may": "mayo", "june": "junio", "july": "julio",
    "august": "agosto", "september": "septiembre", "october": "octubre",
    "november": "noviembre", "december": "diciembre",
}


def _looks_spanish(text: str) -> bool:
    """Heuristic: returns True when ANY ES weekday/month token appears.

    The rule is intentionally narrow — we only translate well-known datetime
    leakage. A single ES weekday/month is enough signal for an EN user.
    """
    if not text:
        return False
    t = text.lower()
    for w in _ES_WEEKDAYS:
        if re.search(rf"\b{w}\b", t):
            return True
    for w in _ES_MONTHS:
        if re.search(rf"\b{w}\b", t):
            return True
    return False


def _has_en_date_tokens(text: str) -> bool:
    """Symmetric to _looks_spanish: returns True when ANY EN weekday/month token appears."""
    if not text:
        return False
    t = text.lower()
    for w in _EN_WEEKDAYS:
        if re.search(rf"\b{w}\b", t):
            return True
    for w in _EN_MONTHS:
        if re.search(rf"\b{w}\b", t):
            return True
    return False


# Common hardcoded skill phrases that need ES translation. Skills emit
# fixed English strings; rather than touch every skill, we translate at
# the response layer when user_lang=es. Keep this list small and only for
# *exact* full-output phrases that show up at fast-path responses.
_HARDCODED_EN_TO_ES = {
    "No active reminders.": "No hay recordatorios activos.",
    "No active reminders to delete.": "No hay recordatorios activos para eliminar.",
    "No scheduled tasks.": "No hay tareas programadas.",
    "No scheduled tasks to delete.": "No hay tareas programadas para eliminar.",
    "No agents found. Use agent_manager(action='create', name='...') to create one.":
        "No hay agentes. Crea uno con agent_manager(action='create', name='...').",
    "No agents found. Create one first with agent_manager(action='create', ...).":
        "No hay agentes. Crea uno primero con agent_manager(action='create', ...).",
}


def _translate_hardcoded_skill_phrases(text: str, user_lang: str) -> tuple[str, bool]:
    """Translate fixed English skill outputs to ES for ES users. Returns
    (text, applied). Only triggers on EXACT matches — we never partial-rewrite
    user-facing strings here."""
    if user_lang != "es" or not text:
        return text, False
    stripped = text.strip()
    es = _HARDCODED_EN_TO_ES.get(stripped)
    if es:
        return es, True
    return text, False


def _enforce_language_consistency(
    text: str, user_lang: str,
) -> tuple[str, dict]:
    """Tiny safety net: translate well-known weekday/month tokens to match
    user_lang. Bidirectional — EN tokens for ES user become ES tokens, and
    ES tokens for EN user become EN tokens. Narrow scope: datetime leakage
    only, not full-response translation. Returns (text, trace)."""
    trace: dict[str, Any] = {"applied": False, "swaps": 0}
    if not text or user_lang not in ("en", "es"):
        return text, trace

    swaps = 0
    if user_lang == "en" and _looks_spanish(text):
        for src, dst in _ES_WEEKDAYS.items():
            new, n = re.subn(rf"\b{re.escape(src)}\b", dst, text, flags=re.IGNORECASE)
            text = new
            swaps += n
        for src, dst in _ES_MONTHS.items():
            new, n = re.subn(rf"\b{re.escape(src)}\b", dst, text, flags=re.IGNORECASE)
            text = new
            swaps += n
    elif user_lang == "es" and _has_en_date_tokens(text):
        # Symmetric reverse: when an ES user gets a response with EN
        # weekday/month tokens leaked in (LLM partially ignored the lang
        # directive), translate just those tokens. The full response stays
        # in whatever language the LLM produced — we do not turn an
        # all-English response into Spanish, only normalize date leakage.
        for src, dst in _EN_WEEKDAYS.items():
            new, n = re.subn(rf"\b{re.escape(src)}\b", dst, text, flags=re.IGNORECASE)
            text = new
            swaps += n
        for src, dst in _EN_MONTHS.items():
            new, n = re.subn(rf"\b{re.escape(src)}\b", dst, text, flags=re.IGNORECASE)
            text = new
            swaps += n

    trace["applied"] = swaps > 0
    trace["swaps"] = swaps
    return text, trace


# ── Factual hallucination guard ──────────────────────────────────────────
# Catches the most damaging class of LLM lie: stating concrete external facts
# (delivery status, prices, package status, news headlines) when no skill
# produced verified data this turn. Distinct from action_announcer (which
# verifies claims about agent ACTIONS) and side_effect_text_gate (claims to
# DO things). This guards claims about EXTERNAL WORLD STATE.

# User-text markers indicating a request for external real-world data.
_FACTUAL_DEMAND_RE = re.compile(
    r"\b("
    # tracking / packages
    r"track(?:ing)?|paquete|package|env[íi]o|shipment|"
    r"c[óo]digo\s+de\s+seguimiento|n[uú]mero\s+de\s+(?:env[íi]o|gu[ií]a)|"
    # real-time data
    r"precio|price|cotizaci[óo]n|valor|rate|"
    r"clima|weather|temperature|"
    # status queries about external entities
    r"estado\s+(?:de|del)|status\s+of|"
    # browse-and-report verbs
    r"busca\s+(?:el|la|los|las)|search\s+for\s+(?:the|my)|"
    r"verifica\s+(?:el|la)|check\s+(?:the|my)"
    r")\b",
    re.IGNORECASE,
)

# Concrete factual assertion patterns — claims of EXTERNAL state with specifics.
_FACTUAL_ASSERTION_PATTERNS = [
    # Delivery / shipment status assertions
    re.compile(
        r"\b(?:ha\s+sido|fue|est[áa]|es)\s+"
        r"(?:entregad[oa]|recibid[oa]|enviad[oa]|despachad[oa]|en\s+tr[áa]nsito|en\s+camino|en\s+aduana)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:has\s+been|was|is)\s+(?:delivered|received|shipped|in[\s-]transit|in\s+customs)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:delivered|entregado)\s+on\s+\w+|"
               r"entregad[oa]\s+(?:el|en)\s+\w+|"
               r"se\s+realiz[óo]\s+el\s+\d", re.IGNORECASE),
    # Price assertions with specific numbers
    re.compile(r"(?:precio\s+(?:actual\s+)?(?:de|del)|price\s+of)\s+\w+\s+(?:es|is)\s*[:=]?\s*\$?\s*\d", re.IGNORECASE),
    re.compile(r"\$\s*\d{2,}", re.IGNORECASE),  # any $ amount
    # Status claims with specific values
    re.compile(r"estado\s+actual\s+es\s+[\"'][^\"']+[\"']", re.IGNORECASE),
    re.compile(r"current\s+status\s+is\s+[\"'][^\"']+[\"']", re.IGNORECASE),
]

# Honest fallback messages — used when factual claim stripped.
_HALLUCINATION_FALLBACK_ES = (
    "No pude obtener esa información en este momento. "
    "El sitio bloqueó la consulta o la captura no se completó. "
    "¿Quieres que lo intente de otra forma?"
)
_HALLUCINATION_FALLBACK_EN = (
    "I could not retrieve that information right now. "
    "The site blocked the query or the capture did not complete. "
    "Want me to try a different approach?"
)


# Tracking-code-like patterns the user might cite. If the user named a
# specific external entity (tracking code, ticker, ISBN, etc.) it MUST
# appear in skill output for the response to be considered grounded.
_USER_ENTITY_RE = re.compile(
    # Tracking codes: 10-25 alphanumeric, mix of upper letters + digits
    r"\b([A-Z]{2}\d{6,12}[A-Z]{2})\b"           # postal codes (LJ040128393CN)
    r"|\b(1Z[A-Z0-9]{16})\b"                    # UPS 1Z...
    r"|\b(\d{12,22})\b"                         # generic long numeric (FedEx etc.)
    r"|\b([A-Z]{4,6}\d{6,10})\b"                # alphanumeric mix
)


def _user_named_entities(user_text: str) -> list[str]:
    """Extract user-specified entity codes (tracking, ticker-like) from text."""
    if not user_text:
        return []
    out: list[str] = []
    for m in _USER_ENTITY_RE.finditer(user_text):
        for g in m.groups():
            if g and g not in out:
                out.append(g)
    return out


def _has_useful_skill_data(skill_results, user_entities: list[str] | None = None) -> bool:
    """True iff at least one skill returned non-trivial useful content
    (not blocked, not [CAPTURE_VALID: false], not error-only).

    Stricter mode (when user_entities is non-empty): a skill output only
    counts as useful for factual grounding if it contains AT LEAST ONE of
    the user-specified entities. This catches the T2-class lie where the
    browser navigated to a home page (lots of text) but never retrieved
    information about the SPECIFIC tracking code the user asked about.
    """
    if not skill_results:
        return False
    for r in skill_results:
        try:
            if not getattr(r, "success", False):
                continue
            out = (getattr(r, "output", "") or "").strip()
            if len(out) < 30:
                continue
            low = out.lower()
            # Filter outputs that LOOK successful but contain no real data
            if "[capture_valid: false]" in low:
                continue
            if "anti-bot" in low or "captcha" in low:
                continue
            # Pure status/configuration responses don't count for factual queries
            sn = (getattr(r, "skill_name", "") or "").lower()
            if sn in ("task_manager", "create_reminder", "agent_manager",
                      "list_reminders", "list_monitors"):
                continue
            # When the user named a specific entity (tracking code etc.),
            # the skill output MUST contain it — otherwise the "useful
            # data" is unrelated page text the LLM will use to fabricate.
            if user_entities:
                if not any(ent.lower() in low for ent in user_entities):
                    continue
            return True
        except Exception:
            continue
    return False


# Words that indicate a delivery/status verdict — these must appear in
# skill output (the page actually showed them) for the response to claim
# them. Without this evidence check, a navigation to a URL that merely
# CONTAINS the tracking code (e.g. `?nums=LJ...`) was being treated as
# "useful data" even though the page never reported a delivery status.
_VERDICT_KEYWORDS_ES = (
    "entregado", "entregada", "en tránsito", "en transito", "en camino",
    "en aduana", "despachado", "recibido", "pendiente",
)
_VERDICT_KEYWORDS_EN = (
    "delivered", "in transit", "in-transit", "shipped", "received",
    "out for delivery", "in customs", "pending",
)


def _response_makes_status_claim(response_text: str) -> str:
    """Return the verdict word (lower-case) the response asserts, or ''."""
    low = response_text.lower()
    for kw in _VERDICT_KEYWORDS_ES + _VERDICT_KEYWORDS_EN:
        if kw in low:
            return kw
    return ""


def _skill_output_supports_verdict(
    verdict: str, skill_results, user_entities: list[str] | None = None,
) -> bool:
    """True iff some successful skill output contains the verdict word in
    its actual page body, in proximity to a user-named entity if any.

    With user_entities: the verdict must appear within 200 chars of an
    entity in the same skill output. This catches the case where 17track
    home page contains the word "delivered" as UI label but the user's
    specific tracking code was NEVER looked up — the LLM stitches the
    unrelated "delivered" string into a fabricated claim. Without entity
    proximity, the verdict word alone (anywhere in body) is not evidence.

    Without user_entities: verdict word anywhere in body is sufficient.
    """
    if not verdict or not skill_results:
        return False
    v = verdict.lower()
    entities = [e.lower() for e in (user_entities or [])]
    for r in skill_results:
        try:
            if not getattr(r, "success", False):
                continue
            out = (getattr(r, "output", "") or "").strip()
            if not out:
                continue
            # Strip the leading "Navigated to: <url>" line — that's just
            # the URL the agent visited, not page content.
            body = re.sub(r"^Navigated to:[^\n]*\n", "", out, count=1, flags=re.IGNORECASE)
            body = re.sub(r"^Title:[^\n]*\n", "", body, count=1, flags=re.IGNORECASE)
            body_low = body.lower()
            if v not in body_low:
                continue
            if not entities:
                return True
            # Entity-proximity check: any entity within 200 chars of any
            # verdict occurrence?
            for ent in entities:
                _e_idx = body_low.find(ent)
                if _e_idx < 0:
                    continue
                _v_idx = body_low.find(v)
                while _v_idx >= 0:
                    if abs(_v_idx - _e_idx) <= 200:
                        return True
                    _v_idx = body_low.find(v, _v_idx + 1)
            # Verdict present but never near an entity → not evidence.
            continue
        except Exception:
            continue
    return False


def enforce_factual_grounding(
    response_text: str, user_text: str, skill_results, user_lang: str = "en",
) -> tuple[str, dict]:
    """If user asked about external real-world data AND no skill produced
    verified data AND response makes a concrete factual claim, replace the
    response with an honest fallback. Returns (cleaned_text, trace).

    This catches the T2-class hallucination: agent fabricates "delivered,
    April 10, Entregado" when no successful capture skill backs the claim.

    Layered check:
      1. User asked about external data (factual demand keywords).
      2. Response makes a verdict claim (delivered/in transit/etc.).
      3. Either no useful skill data OR no skill output supports the
         specific verdict in PAGE BODY (URL parameters don't count).
    """
    trace: dict[str, Any] = {"applied": False, "reason": ""}
    if not response_text or not user_text:
        return response_text, trace

    if not _FACTUAL_DEMAND_RE.search(user_text):
        return response_text, trace

    # Specific layer: response asserts a verdict (delivered, in transit…).
    # If skill body doesn't contain that verdict, strip — even if a skill
    # technically returned "useful data" by length. This catches the case
    # where browser navigated to a URL containing the tracking code in
    # its query string (so the entity check passes) but the page body
    # never showed the actual status verdict.
    verdict = _response_makes_status_claim(response_text)
    user_entities = _user_named_entities(user_text)
    if verdict and not _skill_output_supports_verdict(
        verdict, skill_results, user_entities=user_entities,
    ):
        cleaned = (
            _HALLUCINATION_FALLBACK_ES
            if user_lang == "es"
            else _HALLUCINATION_FALLBACK_EN
        )
        trace["applied"] = True
        trace["reason"] = "verdict_claim_unsupported_by_skill_body"
        trace["verdict"] = verdict
        trace["original_preview"] = response_text[:120]
        return cleaned, trace

    user_entities = _user_named_entities(user_text)
    if _has_useful_skill_data(skill_results, user_entities=user_entities):
        return response_text, trace

    # User asked for external data, no skill returned anything useful.
    # Now check if the response contains a concrete factual assertion.
    for pat in _FACTUAL_ASSERTION_PATTERNS:
        if pat.search(response_text):
            cleaned = (
                _HALLUCINATION_FALLBACK_ES
                if user_lang == "es"
                else _HALLUCINATION_FALLBACK_EN
            )
            trace["applied"] = True
            trace["reason"] = "factual_claim_without_skill_data"
            trace["original_preview"] = response_text[:120]
            return cleaned, trace

    return response_text, trace


# ── Markdown sanitization ────────────────────────────────────────────────
# prime.md §9 forbids markdown in user-facing text. The LLM occasionally
# leaks `![alt](url)` image syntax with broken urls or bullets. Strip them.

# Restrict URL match to non-space, non-`)` chars so a broken-paren image
# (e.g. `![cap]( 2. ![cap]( ...`) doesn't greedily swallow the rest of
# the message. Each `![]()` block stops at the first space.
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^\s)]*\)?")
# Plain markdown links [text](url) — collapse to "text (url)" so the
# user keeps the URL accessible without raw markdown rendering as text
# in Telegram. The `(?<!!)` guard prevents matching after `!` (image).
_MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\((https?://[^\s)]+)\)")
_MARKDOWN_BOLD_ITALIC_RE = re.compile(r"(\*\*|__)(.+?)\1")
_MARKDOWN_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_MARKDOWN_HEADER_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_MARKDOWN_HR_RE = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$", re.MULTILINE)


def sanitize_markdown(response_text: str) -> tuple[str, dict]:
    """Strip markdown leakage that prime.md §9 forbids. Returns (text, trace)."""
    trace: dict[str, Any] = {"applied": False, "stripped": []}
    if not response_text:
        return response_text, trace

    text = response_text
    # Image syntax — strip entirely (path must not appear in user text per §6)
    if _MARKDOWN_IMAGE_RE.search(text):
        text = _MARKDOWN_IMAGE_RE.sub("", text)
        trace["stripped"].append("image")
    # Plain link syntax — collapse `[text](url)` to `text (url)` so the
    # URL stays visible without raw markdown chars rendering literally.
    if _MARKDOWN_LINK_RE.search(text):
        text = _MARKDOWN_LINK_RE.sub(r"\1 (\2)", text)
        trace["stripped"].append("link")
    # Bold/italic — keep inner text
    if _MARKDOWN_BOLD_ITALIC_RE.search(text):
        text = _MARKDOWN_BOLD_ITALIC_RE.sub(r"\2", text)
        trace["stripped"].append("bold_italic")
    # Inline code — keep inner text
    if _MARKDOWN_INLINE_CODE_RE.search(text):
        text = _MARKDOWN_INLINE_CODE_RE.sub(r"\1", text)
        trace["stripped"].append("inline_code")
    # Headers — strip the `#` prefix, keep title text
    if _MARKDOWN_HEADER_RE.search(text):
        text = _MARKDOWN_HEADER_RE.sub("", text)
        trace["stripped"].append("header")
    # Horizontal rules — drop entirely
    if _MARKDOWN_HR_RE.search(text):
        text = _MARKDOWN_HR_RE.sub("", text)
        trace["stripped"].append("hr")

    # Collapse double-spaces from removals
    text = re.sub(r"  +", " ", text)
    # Collapse 3+ newlines to 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()

    if trace["stripped"]:
        trace["applied"] = True
    return text, trace


def apply_final_response_policy(
    response_text: str,
    *,
    user_text: str,
    skill_results,
    user_lang: str = "en",
    chat_id: str = "",
    recent_action_resolver=None,
) -> tuple[str, dict]:
    """Apply the full final-response policy in order. Returns (text, trace).

    Order:
      1. schedule honesty               — strip clock-time scheduling lies
      2. side-effect text gate          — strip unauthorized "I will email" claims
      3. action announcer               — verify remaining claims against
                                          skill_results AND append the
                                          structured ACTIONS block (single
                                          source of truth for what happened)
      4. factual grounding              — strip concrete external-world
                                          claims when no skill data backs them
      5. markdown sanitization          — strip ![...](...), **bold**, etc.
      6. language consistency           — translate ES weekday/month leakage
                                          for EN users (deterministic)

    Layers 1+2 are belt; layer 3 is suspenders. They overlap intentionally
    because layer 3's regex generalizes across phrasings while layers 1+2
    handle specific known-bad cases with curated disclaimer suffixes.

    trace contains the sub-traces from each guard so callers can attach them
    to the request-level DecisionTrace.
    """
    from .action_announcer import apply_action_announcer

    trace: dict[str, Any] = {}

    text, sched = enforce_schedule_honesty(
        response_text, skill_results, user_lang, user_text=user_text,
    )
    trace["schedule_honesty"] = sched

    text, sideeff = enforce_side_effect_text_gate(
        text,
        user_text,
        skill_results,
        chat_id=chat_id,
        user_lang=user_lang,
        recent_action_resolver=recent_action_resolver,
    )
    trace["side_effect_text"] = sideeff

    text, announce = apply_action_announcer(text, skill_results, user_lang=user_lang)
    trace["action_announcer"] = announce

    text, factual = enforce_factual_grounding(
        text, user_text, skill_results, user_lang=user_lang,
    )
    trace["factual_grounding"] = factual

    text, mdtrace = sanitize_markdown(text)
    trace["markdown_sanitization"] = mdtrace

    # Translate hardcoded English skill phrases (e.g. "No active reminders.")
    # before token-level lang consistency runs. Exact-match only.
    text, hp_applied = _translate_hardcoded_skill_phrases(text, user_lang)
    trace["hardcoded_phrase_translation"] = {"applied": hp_applied}

    text, lang = _enforce_language_consistency(text, user_lang)
    trace["language_consistency"] = lang

    return text, trace
