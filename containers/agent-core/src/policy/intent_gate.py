"""Intent boundary gate — single source of truth.

The system, not the LLM, is the final authority on whether the user asked for
a side-effect. This module decides which skill calls are allowed.

Public surface:
  • INTENT_GATE_PATTERNS   — explicit-keyword regex per side-effect skill
  • SIDE_EFFECT_SKILLS     — set of skill names that require explicit intent
  • SKILL_SAFE_ACTIONS     — actions that bypass the gate (read/list/status)
  • intent_gate_check()    — single-call decision for one skill call
  • filter_inferred_side_effects() — bulk filter with stamping

State for the short-context tracker (recent explicit action) and request-
scoped budget lives in `events.handlers` because it is per-process global
mutable state — keeping it there avoids circular imports while keeping the
gate logic itself pure.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

# ── Side-effect skills requiring explicit intent ─────────────────────────
SIDE_EFFECT_SKILLS = {"gmail", "agent_manager", "task_manager"}

# Read-only / lifecycle actions that NEVER require explicit intent.
SKILL_SAFE_ACTIONS: dict[str, set[str]] = {
    "gmail":         {"read", "search", "configure", "list", "status"},
    "agent_manager": {"list", "pause", "resume", "delete", "archive", "status"},
    "task_manager":  {"list", "trigger", "delete", "delete_all", "update",
                      "pause", "resume", "status"},
}

# Reference phrases that point back to the previous turn's explicit action.
REFERENCE_PHRASE_RE = re.compile(
    r"\b(?:haz(?:lo)?\s+(?:lo\s+)?mismo|"
    r"otra\s+vez|de\s+nuevo|repítelo|repite|"
    r"hazlo\s+(?:de\s+)?nuevo|"
    r"do\s+(?:it|that)\s+again|do\s+the\s+same|"
    r"again|once\s+more|same\s+(?:thing|action)|"
    r"como\s+antes|like\s+before)\b",
    re.IGNORECASE,
)

# Email address detector. Used to validate gmail.send recipient comes from
# user-visible content, not LLM imagination.
EMAIL_ADDR_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# ── Explicit-intent regex per side-effect skill ──────────────────────────
INTENT_GATE_PATTERNS: dict[str, re.Pattern] = {
    "gmail": re.compile(
        # send / write / push to email — covers Spanish accent + clitic forms
        # (envía / envíalo / envíamelo / mándalo / mándamelo / mándaselo etc.)
        r"\b(?:env[íi][aá](?:r|me|nos|le|les|lo|la|los|las|melo|mela|selo|sela)?|"
        r"m[aá]nd[aá](?:r|me|nos|le|les|lo|la|los|las|melo|mela|selo|sela)?|"
        r"send|email|e-?mail|gmail|"
        r"correo|mail|inbox|"
        r"reportar|inform[ae]r?(?:me)?|notify|notificar|notificarme|"
        r"escribe(?:le)?\s+a|escr[íi]bele\s+a|"
        r"comparte\s+por\s+correo|share\s+by\s+email)\b",
        re.IGNORECASE,
    ),
    "agent_manager": re.compile(
        # Strict — only creating a sub-agent when the user explicitly says so.
        # "monitor X every hour" / "schedule daily" do NOT match.
        # Requires the literal noun "agente" / "agent" or "sub-agent" /
        # "sub-agente" paired with creation verbs OR "dedicado" / "dedicated".
        r"\b(?:"
        r"crea(?:r|me)?\s+(?:un|una|otro|otra|nuev[oa])?\s*(?:sub-?)?agente|"
        r"create\s+(?:an?|new|another)?\s*(?:sub-?)?agent|"
        r"hazme\s+un\s+(?:sub-?)?agente|h[áa]game\s+un\s+(?:sub-?)?agente|"
        r"set\s+up\s+a(?:n|\s)?\s*(?:sub-?)?agent|"
        r"sub-?agente\b|sub-?agent\b|"
        r"agente\s+(?:dedicado|aut[óo]nomo)|"
        r"dedicated\s+(?:sub-?)?agent|"
        r"autonomous\s+(?:sub-?)?agent"
        r")\b",
        re.IGNORECASE,
    ),
    "task_manager": re.compile(
        r"\b(?:cada\s+(?:hora|d[íi]a|semana|mes|\d+\s*(?:hora[s]?|d[íi]a[s]?|min(?:utos)?|seg))|"
        r"todos\s+los\s+d[íi]as|cada\s+d[íi]a|"
        r"diari[oa]?|diariamente|semanal(?:mente)?|mensual(?:mente)?|"
        r"weekly|daily|hourly|monthly|"
        r"every\s+(?:hour|day|week|month|\d+\s*(?:hours?|days?|weeks?|minutes?))|"
        r"programa(?:r|me)?\s+(?:una|un|que|este|esta)|"
        r"schedule\s+(?:a|the|this|that)|"
        r"recurrente|recurring|peri[óo]dic[oa]|"
        r"automatiza(?:r|me)|automate|automation|"
        r"cre(?:a|á|ame)\s+(?:una|un)\s+(?:tarea|task)|"
        r"create\s+(?:a|the)\s+(?:task|recurring)|"
        r"recordatorio\s+recurrente)\b",
        re.IGNORECASE,
    ),
}

# ── Placeholder content detection (gmail.send guard) ─────────────────────
_PLACEHOLDER_SUBJECTS = frozenset({
    "", "subject", "subject here", "your subject", "your subject here",
    "(no subject)", "no subject", "untitled", "asunto", "sin asunto",
    "sin título", "sin titulo", "title", "titulo", "tu asunto", "tu tema",
    "todo", "tbd", "placeholder", "test", "prueba", "hola", "hello",
    "saludo", "saludos", "n/a", "na", "tema", "asunto del correo",
    "subject of the email", "your message", "tu mensaje",
})
_PLACEHOLDER_BODIES = frozenset({
    "", "body", "body here", "your body", "your body here",
    "test", "prueba", "hello", "hola", "saludo", "saludos",
    "todo", "tbd", "placeholder", "n/a", "na", "tu mensaje",
    "your message here", "message body", "cuerpo del mensaje",
})

_PLACEHOLDER_LIKE_RE = re.compile(
    r"^\s*(?:"
    r"your\s+\w+|tu\s+\w+|"
    r"\[[^\]]*\]|"
    r"<[^>]*>|"
    r"\([^\)]*here\)|"
    r"\.\.\.+|"
    r"saludo[s]?|hello|hi|hola"
    r")\s*[.,;:!?]?\s*$",
    re.IGNORECASE,
)

_USER_CONTENT_REF_RE = re.compile(
    r"\b(?:"
    r"(?:este|ese|esa|esta|esos|esas|el|la|los|las)\s+"
    r"(?:resumen|reporte|informe|texto|art[íi]culo|nota|detalles?|"
    r"an[áa]lisis|resultados?|datos|noticia|cifras?|capturas?|im[áa]genes?|imagen|gr[áa]fico|"
    r"mensaje|email|correo|reporte)|"
    r"(?:this|that|these|those|the)\s+"
    r"(?:summary|report|analysis|article|note|details?|results?|data|"
    r"news|figures?|screenshot|image|chart|message|email)"
    r")\b",
    re.IGNORECASE,
)

# Internal user-prefixes injected by the system that are NOT real user content.
_INTERNAL_USER_PREFIXES = (
    "[INTENT GATE]", "[ROUTING:", "[TAREA",
    "[CORRECTION", "[SYSTEM_CONSTRAINT", "[CAPTURE_VALID",
    "[TOOL_ROUTER_ERROR]", "[CRITICAL", "[REPLAN",
    "Skill results:", "[Resultados de búsqueda",
)


def is_placeholder_subject(s: str) -> bool:
    if not s:
        return True
    s_norm = s.strip().lower()
    if len(s_norm) < 3:
        return True
    if s_norm in _PLACEHOLDER_SUBJECTS:
        return True
    if _PLACEHOLDER_LIKE_RE.match(s_norm):
        return True
    return False


def is_placeholder_body(b: str) -> bool:
    if not b:
        return True
    b_norm = b.strip().lower()
    if len(b_norm) < 10:
        return True
    if b_norm in _PLACEHOLDER_BODIES:
        return True
    if _PLACEHOLDER_LIKE_RE.match(b_norm):
        return True
    return False


def user_message_provides_content(text: str) -> bool:
    """True iff the user's message offers concrete content beyond a send request.

    Heuristics (any one is enough):
      • Long message (≥80 chars).
      • Quoted text — explicit content boundaries.
      • Reference to existing content ("este resumen", "the summary").
      • Multiple sentences (period followed by a word).
      • Colon-prefixed payload ("envía a alice@example.com: <body>") — the user
        explicitly handed us the body inline. We trim the part after ':' and
        accept any non-trivial text. This is the most common short-content
        pattern and was previously blocked by the 80-char floor.
    """
    if not text:
        return False
    t = text.strip()
    if len(t) >= 80:
        return True
    if '"' in t or "”" in t or "“" in t:
        return True
    if _USER_CONTENT_REF_RE.search(t):
        return True
    if re.search(r"[.!?]\s+\w{2,}", t):
        return True
    # Colon-prefixed inline body. Match a colon followed by ≥6 chars of
    # non-trivial text (excluding pure email recipients which look like
    # "to: alice@example.com" — those are recipient declarations, not content).
    m = re.search(r":\s*([^\s].{5,})$", t)
    if m:
        body = m.group(1).strip()
        # Reject if body is just an email address (recipient declaration form)
        if not EMAIL_ADDR_RE.fullmatch(body):
            return True
    return False


# ── Decision API ─────────────────────────────────────────────────────────
# `recent_action_resolver` is an optional callable injected by the caller to
# look up the per-chat recent-explicit-action tracker. Keeping it as a
# parameter lets this module stay pure and decoupled from event handler
# state. Signature: (chat_id: str, skill_name: str) -> dict.

RecentActionResolver = Callable[[str, str], dict]


def intent_gate_check(
    skill_call,
    user_text: str,
    ctx_messages: Optional[list] = None,
    chat_id: str = "",
    recent_action_resolver: Optional[RecentActionResolver] = None,
) -> tuple[bool, str, str]:
    """Decide whether one skill call is authorized.

    Returns (allowed, reason, intent_label).
    intent_label ∈ {'explicit', 'context_allowed', 'inferred_blocked',
                    'non_side_effect'}.
    """
    sn = getattr(skill_call, "skill_name", "")
    if sn not in SIDE_EFFECT_SKILLS:
        return True, "", "non_side_effect"

    args = getattr(skill_call, "arguments", {}) or {}
    action = str(args.get("action") or "").lower().strip()
    safe = SKILL_SAFE_ACTIONS.get(sn, set())
    if action in safe:
        return True, "non_side_effect_action", "non_side_effect"

    pattern = INTENT_GATE_PATTERNS.get(sn)
    explicit_keyword = bool(pattern and pattern.search(user_text or ""))

    has_context_ref = False
    if not explicit_keyword:
        if REFERENCE_PHRASE_RE.search(user_text or "") and recent_action_resolver:
            last_ref = recent_action_resolver(chat_id, sn) or {}
            if last_ref and last_ref.get("action") == action:
                has_context_ref = True
        if not has_context_ref:
            return False, "no_explicit_intent_in_user_message", "inferred_blocked"

    # Recipient + content validation for gmail
    if sn == "gmail":
        user_emails: set[str] = set()
        for em in EMAIL_ADDR_RE.findall(user_text or ""):
            user_emails.add(em.lower())
        if ctx_messages:
            for m in list(ctx_messages)[-4:]:
                if getattr(m, "role", "") not in ("user", "assistant"):
                    continue
                # Skip few-shot examples — they're not real user content,
                # just system-prompt training. Tagged with meta={"fewshot": True}
                # in agent.context.build_context.
                if getattr(m, "meta", {}).get("fewshot"):
                    continue
                for em in EMAIL_ADDR_RE.findall(getattr(m, "content", "") or ""):
                    user_emails.add(em.lower())
        if recent_action_resolver:
            last_act = recent_action_resolver(chat_id, "gmail") or {}
            tracked = (last_act.get("recipient") or "").lower()
            if tracked:
                user_emails.add(tracked)

        args_to = str(args.get("to") or "").strip().lower()
        has_recip = (args_to in user_emails) if args_to else bool(user_emails)
        if not has_recip:
            return False, "missing_recipient", "inferred_blocked"

        effective_action = action if action else "send"
        if effective_action in ("send", "draft"):
            user_provided = user_message_provides_content(user_text)
            if (
                not user_provided
                and ctx_messages
                and REFERENCE_PHRASE_RE.search(user_text or "")
            ):
                for m in list(ctx_messages)[-4:]:
                    if getattr(m, "role", "") != "user":
                        continue
                    # Hard skip: few-shot examples are NOT user content.
                    if getattr(m, "meta", {}).get("fewshot"):
                        continue
                    content = (getattr(m, "content", "") or "").lstrip()
                    if any(content.startswith(p) for p in _INTERNAL_USER_PREFIXES):
                        continue
                    if user_message_provides_content(content):
                        user_provided = True
                        break
            if not user_provided:
                args_subject = str(args.get("subject") or "")
                args_body = str(args.get("body") or "")
                args_have_real = (
                    not is_placeholder_subject(args_subject)
                    and not is_placeholder_body(args_body)
                )
                if not args_have_real:
                    return False, "missing_content", "inferred_blocked"
                # Args look real but user provided nothing → still block.
                return False, "missing_content", "inferred_blocked"

    if has_context_ref:
        return True, "context_reference_match", "context_allowed"
    return True, "explicit_intent_present", "explicit"


def filter_inferred_side_effects(
    skill_calls,
    user_text: str,
    ctx_messages: Optional[list] = None,
    chat_id: str = "",
    recent_action_resolver: Optional[RecentActionResolver] = None,
    record_explicit_action: Optional[Callable[..., None]] = None,
) -> tuple[list, list]:
    """Bulk filter. Returns (allowed, dropped).

    Each dropped entry: (skill_call, reason, intent_label).
    Explicit allowances are stamped via `record_explicit_action` so short-
    reference follow-ups can refer to them.
    """
    allowed: list = []
    dropped: list = []
    for sc in skill_calls or []:
        ok, reason, intent_label = intent_gate_check(
            sc, user_text, ctx_messages, chat_id, recent_action_resolver
        )
        if ok:
            allowed.append(sc)
            if intent_label == "explicit" and record_explicit_action:
                args = getattr(sc, "arguments", {}) or {}
                action = str(args.get("action") or "").lower().strip()
                recipient = str(args.get("to") or "")
                if not recipient and getattr(sc, "skill_name", "") == "gmail":
                    m = EMAIL_ADDR_RE.search(user_text or "")
                    if m:
                        recipient = m.group(0)
                try:
                    record_explicit_action(chat_id, sc.skill_name, action, recipient)
                except Exception:
                    pass
        else:
            dropped.append((sc, reason, intent_label))
    return allowed, dropped
