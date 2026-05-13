"""Response Binding + Honesty Layer.

Goal: every published response must derive from the actual execution of the
current turn. No carry-over from previous turns. No LLM-invented outcomes.
No content from a topic that no skill in this turn actually executed.

Three guards composed in `apply_honesty_layer`:

  1. Categorize this turn's successful skill executions into topics
     (screenshot, email, tracking, task, agent, web_content).

  2. Detect content leakage in the response: text mentioning a topic whose
     supporting skills did not run successfully this turn.

  3. Score severity:
       - "fatal"   — response talks ONLY about a topic with zero matching
                     successful skills, AND user's intent is unrelated.
                     → REPLACE with canonical honesty message.
       - "mild"    — response is mostly grounded but contains stray phrases
                     about an unsupported topic. → STRIP those phrases.
       - "ok"      — response groundable. → pass through.

The honesty layer is intentionally lightweight: pure pattern matching, no
LLM call. It runs BEFORE `policy.response_guard.apply_final_response_policy`
so that the existing guards (action announcer, factual grounding, language
consistency) work on already-clean text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import structlog

logger = structlog.get_logger()


# ── Topic ↔ skill ↔ phrase mapping ────────────────────────────────────────────
#
# Each topic is a coherent capability (screenshot, email, ...). For each topic:
#   patterns: re.Pattern[]  — content that legitimately belongs to this topic
#   skills:   set[str]      — skill names that produce that content
#
# The patterns are deliberately narrow. We DO NOT match things like the bare
# word "browser" or "page" (too generic) — only phrases that were observed
# leaking in the May 2026 dialog: "Invalid capture", "page blocked",
# "screenshot was", "I sent the email", etc.

class Topic(str, Enum):
    SCREENSHOT = "screenshot"
    EMAIL = "email"
    TRACKING = "tracking"
    TASK = "task"
    AGENT = "agent"
    WEB_CONTENT = "web_content"  # generic "I navigated to / fetched / read..."
    REMINDER = "reminder"


_TOPIC_PATTERNS: dict[Topic, list[re.Pattern]] = {
    Topic.SCREENSHOT: [
        # Failure phrases (LLM admitting it failed)
        re.compile(r"\binvalid\s+capture\b", re.I),
        re.compile(r"\bpage\s+blocked\b", re.I),
        re.compile(r"\bcaptura(?:s)?\s+(?:no\s+vál?i?da|inválida|fallida|bloqueada)", re.I),
        re.compile(r"\b(?:la\s+)?captura\s+(?:no\s+contiene|fall(?:ó|aron|a)|salió\s+mal)", re.I),
        re.compile(r"\bscreenshot(?:\s+(?:no\s+vál?i?do|failed|blocked))", re.I),
        re.compile(r"\b(?:no\s+pude|couldn't)\s+(?:capturar|tomar\s+(?:la\s+)?captura|screenshot|capture)\b", re.I),
        re.compile(r"\b(?:login|sign[\-\s]?in)\s+wall\b", re.I),
        re.compile(r"\bcapture[_\s]valid\s*[:=]\s*false\b", re.I),
        # POSITIVE success claims (LLM saying "here is the screenshot" → must be grounded)
        re.compile(r"\b(?:aquí|aqui|acá|aca)\s+(?:está|tienes|te\s+(?:muestro|envío|paso))\s+(?:la\s+)?captura\b", re.I),
        re.compile(r"\b(?:esta|esa)\s+es\s+(?:la\s+)?captura\b", re.I),
        re.compile(r"\b(?:te\s+(?:envié|paso|muestro|adjunto)|adjunto)\s+(?:la\s+)?captura(?:s)?\b", re.I),
        re.compile(r"\b(?:capturé|capturado(?:s)?|capturadas|tomé\s+(?:la\s+)?captura)\b", re.I),
        re.compile(r"\bportada\s+(?:de|del)\s+[a-z0-9\-]+\.[a-z]{2,}", re.I),
        re.compile(r"\bcaptur(?:a|é|ar)\s+(?:de|del)\s+(?:la\s+)?(?:portada|página|sitio)\b", re.I),
        re.compile(r"\bhere\s+(?:is|are)\s+(?:the|your)\s+screenshot", re.I),
        re.compile(r"\bI\s+(?:took|captured|grabbed)\s+(?:the|a)\s+screenshot", re.I),
    ],
    Topic.EMAIL: [
        re.compile(r"\b(?:te\s+envié|envié|envio|enviado|mandé|envié\s+ya)\s+(?:un\s+|el\s+|tu\s+|el\s+correo\s+)?(?:correo|email|mail)\b", re.I),
        re.compile(r"\b(?:I\s+sent|I'?ve\s+sent|email\s+sent|sent\s+the?\s+email)\b", re.I),
        re.compile(r"\b(?:correo|email)\s+(?:enviado|delivered|sent|de\s+prueba\s+enviado)\b", re.I),
        re.compile(r"\bgmail\s+(?:envió|delivered|skill)\b", re.I),
    ],
    Topic.TRACKING: [
        re.compile(r"\b17\s*track(?:\.net)?\b", re.I),
        re.compile(r"\b(?:tracking|rastreo|seguimiento)\s+(?:de(?:l)?\s+(?:paquete|envío)|number|code|n[uú]mero)\b", re.I),
        # Generic shipment-status claims — relaxed to match informal Spanish
        re.compile(r"\bestado\s+(?:de(?:l)?|de\s+(?:tu|mi|su))\s+(?:paquete|envío|env[íi]o|shipment)", re.I),
        re.compile(r"\b(?:tu|mi|el)\s+paquete\s+(?:est[áa]\s+(?:en|listo)|llega|fue\s+entregado|en\s+tr[aá]nsito)", re.I),
        re.compile(r"\bpaquete\s+en\s+tr[aá]nsito\b", re.I),
        re.compile(r"\bshipment\s+status\b", re.I),
        re.compile(r"\b(?:package|delivery)\s+(?:status|location|update|arrived|delivered)\b", re.I),
    ],
    Topic.TASK: [
        re.compile(r"\btarea\s+(?:programada|creada|actualizada|eliminada|borrada|pausada|reanudada)\b", re.I),
        re.compile(r"\bSCHEDULE_TYPE\b", re.I),
        re.compile(r"\bdaily\s+at\s+\d", re.I),
        re.compile(r"\bevery\s+\d+\s*(?:h|hour|min|day|week)", re.I),
        re.compile(r"\bfirst\s+run:|next\s+run:", re.I),
        re.compile(r"\btask\s+(?:created|updated|deleted|paused|resumed|scheduled|cancelled)\b", re.I),
    ],
    Topic.REMINDER: [
        re.compile(r"\brecordatorio\s+(?:creado|guardado|programado|eliminado|borrado)\b", re.I),
        re.compile(r"\breminder\s+(?:created|saved|scheduled|deleted|cancelled)\b", re.I),
    ],
    Topic.AGENT: [
        re.compile(r"\b(?:sub[\-\s]?agente|subagente)\s+(?:creado|nuevo|listo|activo|pausado)\b", re.I),
        re.compile(r"\b(?:I\s+)?created\s+(?:a\s+)?(?:sub[\-\s]?)?agent\b", re.I),
    ],
    Topic.WEB_CONTENT: [
        re.compile(r"\b(?:según|seg[uú]n|de acuerdo a|en|de)\s+(?:la\s+)?(?:página|sitio|noticia)\s+(?:de\s+)?[a-z0-9\-]+\.[a-z]{2,}", re.I),
        re.compile(r"\b(?:headline|titular|article|nota)\s+(?:says|dice|indica)\b", re.I),
    ],
}

# Skills that supply each topic (success of any one of these grounds the topic)
_TOPIC_SKILLS: dict[Topic, set[str]] = {
    Topic.SCREENSHOT:   {"browser"},
    Topic.EMAIL:        {"gmail", "send_email", "email"},
    Topic.TRACKING:     {"browser", "fetch_url", "http_request", "web_search"},
    Topic.TASK:         {"task_manager"},
    Topic.REMINDER:     {"create_reminder", "delete_reminder", "list_reminders", "reminders"},
    Topic.AGENT:        {"agent_manager"},
    Topic.WEB_CONTENT:  {"browser", "fetch_url", "http_request", "web_search", "web_fetch"},
}

# Mapping intent action_type → "primary topic" of the turn (for severity scoring)
_INTENT_TO_TOPIC: dict[str, Topic] = {
    "browser_navigation":    Topic.WEB_CONTENT,
    "browser_form_workflow": Topic.WEB_CONTENT,
    "browser_web_workflow":  Topic.WEB_CONTENT,
    "browser_package_check": Topic.TRACKING,
    "screenshot_capture":    Topic.SCREENSHOT,
    "email_send":            Topic.EMAIL,
    "task_create":           Topic.TASK,
    "task_update":           Topic.TASK,
    "task_delete":           Topic.TASK,
    "task_list":             Topic.TASK,
    "task_management":       Topic.TASK,
    "reminder_create":       Topic.REMINDER,
    "reminder_delete":       Topic.REMINDER,
    "agent_create":          Topic.AGENT,
}


# ── Outcome data class ────────────────────────────────────────────────────────

class OutcomeStatus(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    BLOCKED = "blocked"


@dataclass
class ExecutionOutcome:
    skill_name: str
    status: OutcomeStatus
    reason: str = ""        # for failure/blocked
    output_snippet: str = ""  # first 240 chars of skill output
    artifacts: list[str] = field(default_factory=list)  # screenshot/file paths
    domain: str = ""        # extracted from output if present


_SHOT_RE = re.compile(r"(/data/screenshots/screenshot_\d+\.png)")
_DOMAIN_IN_OUTPUT_RE = re.compile(r"\b([a-z0-9][a-z0-9\-]*\.[a-z]{2,})(?:/|\b)", re.I)


def summarize_outcomes(skill_results: list[Any]) -> list[ExecutionOutcome]:
    """Convert SkillResult[] → ExecutionOutcome[].

    Maps existing SkillResult fields:
      success=True               → SUCCESS
      success=False + "blocked"  → BLOCKED
      success=False              → FAILURE

    Detects [CAPTURE_VALID: false] in screenshot output and downgrades to FAILURE.
    """
    outcomes: list[ExecutionOutcome] = []
    for r in skill_results or []:
        skill_name = getattr(r, "skill_name", "") or ""
        success = bool(getattr(r, "success", False))
        output = getattr(r, "output", "") or ""
        error = getattr(r, "error", "") or ""

        # Map status
        status = OutcomeStatus.SUCCESS if success else OutcomeStatus.FAILURE
        reason = ""
        if not success:
            err_lower = (error or output or "").lower()
            if any(m in err_lower for m in (
                "blocked", "domain_lock", "active_lock=", "pre_execution_guard",
                "tool_violation", "domain_drift", "hijack",
            )):
                status = OutcomeStatus.BLOCKED
            reason = (error or output[:200] or "skill failed").strip()

        # Browser invalid-capture: success=True but output declares invalid → FAILURE
        if status == OutcomeStatus.SUCCESS and "[CAPTURE_VALID: false]" in output:
            status = OutcomeStatus.FAILURE
            reason = "Screenshot capture failed (page blocked or content not present)."

        # Extract artifacts (screenshot paths)
        artifacts = _SHOT_RE.findall(output)

        # Extract a domain heuristic from output (first match)
        domain = ""
        m = _DOMAIN_IN_OUTPUT_RE.search(output)
        if m:
            d = m.group(1).lower()
            if "." in d and not d.endswith((".png", ".jpg", ".jpeg")):
                domain = d

        outcomes.append(ExecutionOutcome(
            skill_name=skill_name,
            status=status,
            reason=reason[:300],
            output_snippet=output[:240],
            artifacts=artifacts,
            domain=domain,
        ))
    return outcomes


def _topics_grounded(outcomes: list[ExecutionOutcome]) -> set[Topic]:
    """Return topics that have at least one SUCCESSFUL outcome supporting them."""
    successful_skills = {o.skill_name for o in outcomes if o.status == OutcomeStatus.SUCCESS}
    grounded: set[Topic] = set()
    for topic, skills in _TOPIC_SKILLS.items():
        if successful_skills & skills:
            grounded.add(topic)
    return grounded


def _topics_mentioned(response_text: str) -> dict[Topic, list[str]]:
    """Find which topics the response text talks about.

    Returns {topic: [matched_phrases...]}. Empty dict if no topic detected
    (likely a generic conversational response).
    """
    found: dict[Topic, list[str]] = {}
    if not response_text:
        return found
    for topic, patterns in _TOPIC_PATTERNS.items():
        hits = []
        for p in patterns:
            m = p.search(response_text)
            if m:
                hits.append(m.group(0))
        if hits:
            found[topic] = hits
    return found


# ── Honesty Layer ─────────────────────────────────────────────────────────────

# Canonical messages live in communication/phrases.py — English-only
# source of truth. Translation to user_lang happens at publish time via
# communication.translator. These dicts are kept as legacy shims for any
# external code that still imports them; new code should use
# `phrases.pick(...)` (sync, English) or `translator.apick(...)` (async,
# translates).
from ..communication.phrases import pick as _pick_phrase  # noqa: E402

CANONICAL_FAILURE_MSG = {"en": "I couldn't complete that. Want me to try a different angle?"}
CANONICAL_BLOCKED_TEMPLATE = {"en": "I tried {action}, but {reason}."}


def _action_label(intent: Any, user_lang: str) -> str:
    """Friendly label for the user's intended action, given an intent object."""
    action_type = getattr(intent, "action_type", "") if intent is not None else ""
    es = user_lang == "es"
    table = {
        "browser_navigation":    ("la navegación" if es else "the navigation"),
        "browser_form_workflow": ("la solicitud" if es else "the request"),
        "browser_web_workflow":  ("la consulta web" if es else "the web request"),
        "browser_package_check": ("el seguimiento del paquete" if es else "the package tracking"),
        "screenshot_capture":    ("la captura" if es else "the screenshot"),
        "email_send":            ("el envío del correo" if es else "the email"),
        "task_create":           ("crear la tarea" if es else "creating the task"),
        "task_update":           ("actualizar la tarea" if es else "updating the task"),
        "task_delete":           ("eliminar la tarea" if es else "deleting the task"),
        "task_management":       ("la gestión de tarea" if es else "the task change"),
    }
    return table.get(action_type, ("la acción solicitada" if es else "the requested action"))


def _action_suggestion(intent: Any, user_lang: str) -> str:
    """Phase 4 (UX): concrete next-step for the user when an action failed.

    Returns a short clause like "Si quieres, busco la info en otra fuente" — paired
    with the failure phrase to avoid the empty "do you want to try differently?".
    Empty string when no specific suggestion fits.
    """
    action_type = getattr(intent, "action_type", "") if intent is not None else ""
    es = user_lang == "es"
    table = {
        "browser_navigation":    ("Si quieres, pruebo otra fuente o una URL específica."
                                  if es else "I can try another source or a specific URL."),
        "browser_web_workflow":  ("Si quieres, busco la información en otra fuente."
                                  if es else "I can look it up in another source."),
        "browser_package_check": ("Prueba pegando el código directamente en el sitio del courier."
                                  if es else "Try pasting the code directly on the courier's site."),
        "screenshot_capture":    ("Si me envías otra URL, lo intento de nuevo."
                                  if es else "Send me another URL and I'll try again."),
        "email_send":            ("Si quieres, lo redacto y tú lo envías manualmente."
                                  if es else "I can draft it and you send it manually."),
        "task_create":           ("Si quieres, ajustamos el intervalo o el destinatario."
                                  if es else "We can adjust the interval or the recipient."),
    }
    return table.get(action_type, "")


def _strip_leaked_sentences(text: str, leaked_topics: set[Topic]) -> str:
    """Remove sentences that match leaked-topic patterns.

    A "sentence" here is any chunk separated by ., !, ?, or newline.
    Keeps non-matching sentences. If everything gets stripped, returns "".
    """
    if not leaked_topics:
        return text
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    keep: list[str] = []
    for sent in sentences:
        s = sent.strip()
        if not s:
            continue
        leaked = False
        for topic in leaked_topics:
            for pat in _TOPIC_PATTERNS.get(topic, []):
                if pat.search(s):
                    leaked = True
                    break
            if leaked:
                break
        if not leaked:
            keep.append(s)
    return " ".join(keep).strip()


def apply_honesty_layer(
    response_text: str,
    *,
    skill_results: list[Any] | None = None,
    user_text: str = "",
    user_lang: str = "en",
    action_intent: Any = None,
    user_attributes: dict[str, str] | None = None,
) -> tuple[str, dict]:
    """Final pre-publish guard.

    Decision flow:
      A. No skill_results AND no action_intent      → pass through (conversational)
      B. Response mentions topic X, but X has zero  → STRIP those sentences.
         successful supporting skills.                If response becomes empty
                                                      AND user committed to action
                                                      → REPLACE with canonical msg.
      C. User intent committed to action of topic Y → if response doesn't
         but no skill in topic Y ran successfully     mention success of Y AND
                                                      response mentions other
                                                      topics ungrounded → REPLACE.
      D. Otherwise pass through.

    Returns (clean_text, trace_dict). Trace contains:
      - status: "passthrough" | "stripped" | "replaced" | "canonical_blocked"
      - leaked_topics: list[str]
      - grounded_topics: list[str]
      - reason: human-readable explanation
    """
    trace: dict[str, Any] = {
        "status": "passthrough",
        "leaked_topics": [],
        "grounded_topics": [],
        "reason": "",
    }
    text = response_text or ""

    # ── Phase 5/8 — User-attribute truth override (runs FIRST) ───────────────
    # Conversational turns ("what's my color?") have no skills and no intent
    # but can still leak DB-contradicting answers. Apply this check before
    # the "passthrough" short-circuit below.
    _attr_check = _check_user_attribute_consistency(text, user_attributes, user_lang)
    if _attr_check is not None:
        replacement, attr_trace = _attr_check
        trace.update(attr_trace)
        return replacement, trace

    # Path A: conversational, nothing to bind
    if not skill_results and not action_intent:
        trace["reason"] = "no_skill_results_no_intent_passthrough"
        return text, trace

    outcomes = summarize_outcomes(skill_results or [])
    grounded = _topics_grounded(outcomes)
    mentioned = _topics_mentioned(text)
    trace["grounded_topics"] = sorted(t.value for t in grounded)
    trace["mentioned_topics"] = sorted(t.value for t in mentioned.keys())

    # ── Path C: action commitment but ZERO grounding for the intended topic ──
    intent_committed = bool(getattr(action_intent, "action_commitment", False))
    intent_action_type = getattr(action_intent, "action_type", "") or ""
    intent_topic = _INTENT_TO_TOPIC.get(intent_action_type)

    # If user committed to an action AND nothing succeeded, the most honest
    # response is the canonical "I couldn't do X" — but ONLY when the LLM's
    # response does NOT already match this honesty (i.e. it's pretending to
    # have done something it didn't). We detect "pretending" by the LLM
    # mentioning a topic with no grounding.
    if intent_committed and intent_topic is not None and intent_topic not in grounded:
        # Did any skill actually fail or get blocked?
        failed_outcomes = [o for o in outcomes if o.status != OutcomeStatus.SUCCESS]
        block_reasons = [o.reason for o in failed_outcomes if o.status == OutcomeStatus.BLOCKED]
        # If the response mentions UNGROUNDED topics, it's likely making things up
        ungrounded_mentions = [t for t in mentioned.keys() if t not in grounded]
        if ungrounded_mentions:
            # REPLACE with canonical English message. Translation to user_lang
            # happens post-honesty in _safe_publish_response, gated on
            # trace["__canonical_en"] = True.
            action = _action_label(action_intent, user_lang)
            _seed = (user_text or "")[:120]
            if block_reasons:
                reason_str = block_reasons[0][:200]
                replacement = _pick_phrase(
                    "action_blocked", seed=_seed,
                    action=action, reason=reason_str,
                )
            else:
                replacement = _pick_phrase("generic_failure", seed=_seed)
            # Phase 4: append a concrete next-step suggestion when we have one.
            _suggestion = _action_suggestion(action_intent, user_lang)
            if _suggestion and _suggestion not in replacement:
                replacement = replacement.rstrip(" .") + ". " + _suggestion
            trace.update({
                "status": "replaced",
                "leaked_topics": [t.value for t in ungrounded_mentions],
                "reason": "intent_topic_ungrounded_with_leakage",
                "__canonical_en": True,
            })
            logger.warning(
                "honesty_layer.replaced",
                intent_topic=intent_topic.value,
                grounded=trace["grounded_topics"],
                leaked=trace["leaked_topics"],
                user_lang=user_lang,
            )
            return replacement, trace

    # ── Path B: strip sentences mentioning leaked topics ─────────────────────
    leaked = {t for t in mentioned.keys() if t not in grounded}
    if leaked:
        # Don't strip the intent topic itself even if ungrounded — the agent
        # SHOULD say it couldn't do X, that's part of the honest path. Strip
        # only OTHER topics that pollute the answer.
        strip_targets = {t for t in leaked if t != intent_topic}
        if strip_targets:
            stripped = _strip_leaked_sentences(text, strip_targets)
            if stripped and stripped != text:
                trace.update({
                    "status": "stripped",
                    "leaked_topics": [t.value for t in strip_targets],
                    "reason": "stripped_unsupported_topic_sentences",
                })
                logger.info(
                    "honesty_layer.stripped",
                    leaked=trace["leaked_topics"],
                    grounded=trace["grounded_topics"],
                    chars_removed=len(text) - len(stripped),
                )
                return stripped, trace
            elif not stripped:
                # All content was leaked — replace with canonical English.
                msg = _pick_phrase(
                    "generic_failure", seed=(user_text or "")[:120],
                )
                trace.update({
                    "status": "replaced",
                    "leaked_topics": [t.value for t in strip_targets],
                    "reason": "all_content_leaked",
                    "__canonical_en": True,
                })
                logger.warning(
                    "honesty_layer.replaced_all_leaked",
                    leaked=trace["leaked_topics"],
                )
                return msg, trace

    trace["reason"] = "passthrough_grounded"

    # ── Phase 1/4/8: data grounding + capability/negative-claim verification ──
    text, v2_trace = _apply_v2_verifications(
        text,
        skill_results=skill_results or [],
        outcomes=outcomes,
        user_lang=user_lang,
        action_intent=action_intent,
        user_attributes=user_attributes,
        user_text=user_text,
    )
    if v2_trace.get("status") and v2_trace["status"] != "passthrough":
        trace.update(v2_trace)
    return text, trace


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1/4/8 — Honesty Layer v2: data grounding + claim verification
# ─────────────────────────────────────────────────────────────────────────────

# Currency / numeric / percentage tokens. We look for tokens that the agent
# is most likely to fabricate when its data source failed: prices, percentages,
# 4+ digit numbers, dates.
_NUMERIC_PRICE_RE = re.compile(
    r"(?:R\$|\$|US\$|USD|EUR|€|£|CL\$|CLP)\s*\d[\d.,]*"
    r"|\d{1,3}(?:[.,]\d{3})+(?:[.,]\d+)?"   # 1.234,56 / 1,234.56 / 400,095.75
    r"|\d+[.,]\d+\s*(?:%|por\s*ciento)"
    r"|\d{4,}",                              # bare 4+ digit numbers
    re.IGNORECASE,
)

# Capability disclaimers — patterns the agent uses when it falsely claims its
# own skill doesn't support a feature it just successfully exercised.
_CAPABILITY_DISCLAIMER_PATTERNS = [
    (re.compile(
        r"\btask_manager\s+(?:solo|only|simplemente)\s+(?:soporta|supports)\s+intervalos\b", re.I),
     "task_manager_intervals_only"),
    (re.compile(
        r"\b(?:no\s+soporta|doesn'?t\s+support)\s+(?:hor[ao]s?|fixed\s+times?|cron|specific\s+time)", re.I),
     "fixed_time_unsupported"),
    (re.compile(
        r"\bcorre\s+cada\s+N\s+horas\s+desde\s+(?:el\s+)?(?:momento\s+de\s+creación|creation)", re.I),
     "interval_only_disclaimer"),
    (re.compile(
        r"\b(?:no\s+puedo|cannot|can'?t)\s+(?:programar|schedule|set)\s+(?:a\s+)?(?:una\s+)?hora\s+(?:fija|específica|specific)", re.I),
     "cannot_schedule_fixed_time"),
]

# Negative claim phrases — the agent saying it refused or could not do X.
_NEGATIVE_CLAIM_PATTERNS = [
    re.compile(r"\b(?:no\s+pude|I\s+(?:could\s*not|couldn'?t|cannot|can'?t))\b[^.!?\n]{0,140}", re.I),
    re.compile(r"\b(?:I\s+refuse[d]?|me\s+(?:negué|nego)|no\s+(?:permito|admito))\b[^.!?\n]{0,140}", re.I),
    re.compile(r"\b(?:no\s+es\s+posible|not\s+possible|forbidden|prohibido|bloqueado)\b[^.!?\n]{0,140}", re.I),
]


def _check_memory_fabrication(
    text: str,
    user_text: str,
    stored: dict[str, str] | None,
    user_lang: str,
) -> tuple[str, dict] | None:
    """B7 fix: detect hallucinated "you said X" / "antes me dijiste X" claims
    where X does not appear in the current user_text and is not a stored
    attribute value. The LLM sometimes pulls X from old episodic memory, which
    is exactly the failure mode F6a hit.

    Strategy:
    - Find phrases of the form "antes me dijiste <value>", "ahora dices <value>",
      "you said <value>", "now you're saying <value>".
    - For each captured <value>: if it's NOT in user_text AND NOT in stored
      attribute values → that sentence is fabricated → strip it.
    - If everything was fabricated → fall back to canonical via attribute check.

    Returns (cleaned_text, trace) when stripping happened, else None.
    """
    if not text:
        return None
    # SAFE: when stored is None or empty, we don't have ground truth to
    # compare against. The check would over-strip. Skip it.
    if not stored:
        return None
    haystack_user = (user_text or "").lower()
    stored_lowers = {v.lower() for v in stored.values() if v}

    # Patterns capturing the quoted value the LLM is claiming the user said.
    # We look for short single-word values to avoid matching legitimate quotes.
    patterns = [
        re.compile(r"\b(?:antes|ant[eé]riormente)\s+me\s+dijiste\s+(?:que\s+(?:era|es|fue|se\s+llama)\s+)?(?P<v>[A-Za-zÁÉÍÓÚÜÑáéíóúüñ\-]{2,30})\b", re.I),
        re.compile(r"\bahora\s+dices\s+(?:que\s+(?:es|era|se\s+llama)\s+)?(?P<v>[A-Za-zÁÉÍÓÚÜÑáéíóúüñ\-]{2,30})\b", re.I),
        re.compile(r"\byou\s+said\s+(?:it\s+was\s+|your\s+\w+\s+was\s+)?(?P<v>[A-Za-z\-]{2,30})\b", re.I),
        re.compile(r"\bnow\s+you'?re?\s+saying\s+(?P<v>[A-Za-z\-]{2,30})\b", re.I),
    ]

    fabricated_values: list[str] = []
    for pat in patterns:
        for m in pat.finditer(text):
            value = (m.group("v") or "").strip().rstrip(".,;:!?")
            if not value:
                continue
            value_l = value.lower()
            # Allowed if literally in user_text OR matches a stored attribute
            if value_l in haystack_user:
                continue
            if value_l in stored_lowers:
                continue
            # Fabrication suspected — but skip very common short words that
            # might match incidentally.
            if value_l in {"yes", "no", "ok", "sí", "si"}:
                continue
            fabricated_values.append(value)

    if not fabricated_values:
        return None

    # Strip every sentence that contains any fabricated value.
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    keep = []
    for sent in sentences:
        if any(v.lower() in sent.lower() for v in fabricated_values):
            continue
        if sent.strip():
            keep.append(sent.strip())
    cleaned = " ".join(keep).strip()

    trace = {
        "status": "v2_memory_fabrication_stripped",
        "v2_violations": ["memory_fabrication"],
        "fabricated_values": fabricated_values[:5],
        "reason": "response_quoted_value_not_in_user_text_or_stored_attributes",
    }

    if not cleaned:
        # Whole response was fabricated — defer to attribute recap if we
        # have stored attributes; otherwise empty (caller falls back).
        if stored:
            from ..communication.phrases import pick as _pp
            header = _pp("attribute_recap_header", seed="|".join(fabricated_values[:3]))
            lines = [header]
            for k, v in sorted(stored.items()):
                lines.append(f"- {k}: {v}")
            cleaned = "\n".join(lines)
            trace["__canonical_en"] = True
        else:
            return None

    logger.warning(
        "honesty_layer_v2.memory_fabrication_stripped",
        fabricated=fabricated_values[:5],
        chars_removed=len(text) - len(cleaned),
    )
    return cleaned, trace


def _check_user_attribute_consistency(
    text: str, stored: dict[str, str] | None, user_lang: str,
) -> tuple[str, dict] | None:
    """Phase 5/8: if the response asserts a value for a tracked user attribute
    (cat name, favourite colour, user name, email) that DIFFERS from what's
    stored in user_attributes, override with the stored value. The DB is
    authoritative — the conversation is not.

    Caller passes the per-turn snapshot of stored attributes (sync-fetched
    in _safe_publish_response). Returns None when no override needed.
    """
    if not text or not stored:
        return None

    # Map attribute keys to the patterns that EXTRACT a claimed value
    # from a response. Each pattern uses a single capturing group `value`.
    es = (user_lang == "es")
    extractors: list[tuple[str, re.Pattern]] = [
        ("pet_cat", re.compile(
            r"\b(?:tu\s+gato\s+se\s+llama|your\s+cat'?s?\s+name\s+is)\s+"
            r"(?P<value>[A-Za-zÁÉÍÓÚÜÑáéíóúüñ][\w\-]{0,40})\b",
            re.IGNORECASE)),
        ("favourite_colour", re.compile(
            r"\b(?:tu\s+color\s+favorito\s+es|your\s+favou?rite\s+colou?r\s+is)\s+"
            r"(?P<value>[A-Za-zÁÉÍÓÚÜÑáéíóúüñ][\w\-]{0,40})\b",
            re.IGNORECASE)),
        ("user_name", re.compile(
            r"\b(?:te\s+llamas|your\s+name\s+is)\s+"
            r"(?P<value>[A-ZÁÉÍÓÚÑ][\wáéíóúüñ\-]{1,40})\b",
            re.IGNORECASE)),
        ("email", re.compile(
            r"\b(?:tu\s+(?:email|correo)\s+es|your\s+email\s+is)\s+"
            r"(?P<value>[\w.+\-]+@[\w\-]+\.[\w.\-]+)",
            re.IGNORECASE)),
    ]
    # Also detect bare-value answers ("Rojo." / "Pixel.") when this turn's
    # user_text was a direct attribute query — we can't tell from `text`
    # alone, so defer; bare answers are flagged below if they match one of
    # the alternative tracked-value tokens.
    bare_value_check_keys = {"favourite_colour", "pet_cat", "user_name"}

    violation = None
    for key, pat in extractors:
        m = pat.search(text)
        if not m:
            continue
        claimed = (m.group("value") or "").strip()
        truth = stored.get(key)
        if not truth:
            continue
        if claimed.lower() != truth.lower():
            violation = (key, claimed, truth)
            break

    # Bare-value short answer (e.g. "Rojo.") — when the response is a
    # single word AND it does not match ANY of the stored attribute values,
    # the agent is likely answering an attribute query with a fabricated
    # value. We can't tell from the response alone which attribute the
    # user asked about, so emit a generic stored-truth recap.
    if not violation:
        stripped = text.strip().rstrip(".!?,").strip()
        if (
            1 <= len(stripped.split()) <= 2
            and re.fullmatch(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ\-]{2,40}", stripped)
        ):
            lower_resp = stripped.lower()
            stored_lowers = {v.lower() for v in stored.values() if v}
            if lower_resp not in stored_lowers and stored_lowers:
                # Bare answer didn't match any stored truth → likely fabricated.
                # Use a generic "store" recap instead of a misleading per-attr msg.
                violation = ("__bare_mismatch__", stripped, "")

    if violation is None:
        return None
    key, claimed, truth = violation
    if key == "__bare_mismatch__":
        # Generic recap: list every stored attribute so the user sees the truth.
        header = _pick_phrase("attribute_recap_header", seed=claimed[:60])
        lines = [header]
        for k, v in sorted(stored.items()):
            lines.append(f"- {k}: {v}")
        replacement = "\n".join(lines)
    else:
        # English label — translator handles localising at publish time.
        label = {
            "pet_cat": "cat's name",
            "favourite_colour": "favourite colour",
            "user_name": "name",
            "email": "email",
        }.get(key, key)
        replacement = _pick_phrase(
            "attribute_truth_single", seed=key + "|" + truth,
            label=label, truth=truth,
        )
    trace = {
        "status": "v2_attribute_truth_override",
        "v2_violations": [f"attribute_truth_mismatch:{key}"],
        "key": key,
        "claimed": claimed,
        "truth": truth,
        "reason": "response_asserts_value_different_from_stored_user_attribute",
        "__canonical_en": True,
    }
    logger.warning(
        "honesty_layer_v2.attribute_truth_override",
        key=key, claimed=claimed, truth=truth,
    )
    return replacement, trace


def _apply_v2_verifications(
    text: str,
    *,
    skill_results: list[Any],
    outcomes: list[ExecutionOutcome],
    user_lang: str,
    action_intent: Any,
    user_attributes: dict[str, str] | None = None,
    user_text: str = "",
) -> tuple[str, dict]:
    """Three additional checks layered on top of the topic-grounding logic.

    Returns (possibly_modified_text, trace). trace.status defaults to
    "passthrough" when nothing was modified; v2_replaced / v2_stripped /
    v2_capability_override otherwise.
    """
    trace: dict[str, Any] = {"status": "passthrough", "v2_violations": []}
    if not text:
        return text, trace
    es = (user_lang == "es")

    # ── Check (z): User-attribute truth override (Phase 5/8) ──────────────────
    # Highest-priority guard: if response asserts a value for a tracked user
    # attribute that contradicts what's stored in user_attributes DB, the DB
    # is authoritative. Replace the response with the stored truth.
    _attr_check = _check_user_attribute_consistency(text, user_attributes, user_lang)
    if _attr_check is not None:
        return _attr_check

    # ── Check (z2): Memory fabrication guard (B7 fix) ─────────────────────────
    # Catch hallucinated "you said X" claims where X is NOT in the current
    # user_text and NOT a stored attribute value. The LLM sometimes pulls X
    # from old episodic memory. Strip those sentences.
    _mem_fab_check = _check_memory_fabrication(text, user_text, user_attributes, user_lang)
    if _mem_fab_check is not None:
        return _mem_fab_check

    # ── Check (a): Capability claim verification ──────────────────────────────
    # If the response says "task_manager doesn't support fixed times" but
    # task_manager actually succeeded with at_time set in this turn, REPLACE.
    for pat, label in _CAPABILITY_DISCLAIMER_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        # Did task_manager run successfully AND is at_time present in any output?
        tm_success = any(
            (getattr(r, "skill_name", "") or "").lower() == "task_manager"
            and bool(getattr(r, "success", False))
            for r in skill_results
        )
        # Look for at_time evidence in skill outputs
        evidence = ""
        for r in skill_results:
            out = getattr(r, "output", "") or ""
            if "at_time" in out.lower() or "hora chile" in out.lower() or re.search(
                r"\bnext\s*[_\s]?run\b.*\d{2}:\d{2}", out, re.I
            ):
                evidence = out[:200]
                break
        if tm_success and evidence:
            # Fabricated capability disclaimer. Replace whole response with
            # an honest success message — canonical English, translator
            # localises post-honesty.
            replacement = _pick_phrase(
                "task_scheduled", seed=evidence[:60],
                detail=evidence[:120] if evidence else "",
            )
            trace.update({
                "status": "v2_capability_override",
                "violation": label,
                "v2_violations": [label],
                "reason": "capability_disclaimer_contradicts_successful_execution",
                "__canonical_en": True,
            })
            logger.warning(
                "honesty_layer_v2.capability_override",
                label=label,
                evidence_excerpt=evidence[:120],
            )
            return replacement, trace

    # ── Check (b): Data grounding for numeric tokens ──────────────────────────
    # For every price / large number in the response, the same number must
    # appear in at least one skill output. Otherwise → STRIP that sentence.
    numeric_tokens = _NUMERIC_PRICE_RE.findall(text)
    if numeric_tokens:
        # Build the haystack from successful skill outputs only — invented
        # numbers in failed-output won't ground a successful claim.
        haystack = " ".join(
            (getattr(r, "output", "") or "")
            for r in skill_results
            if bool(getattr(r, "success", False))
        )
        ungrounded = []
        for tok in numeric_tokens:
            # Normalise: strip currency/whitespace, compare canonical digits.
            digits = re.sub(r"[^\d.,]", "", tok)
            if not digits:
                continue
            # Allow ±2-char fuzziness in formatting (1,234 vs 1234)
            digits_no_punct = re.sub(r"[.,]", "", digits)
            if (digits in haystack) or (digits_no_punct in re.sub(r"[.,]", "", haystack)):
                continue
            ungrounded.append(tok)
        if ungrounded:
            # Strip every sentence that contains an ungrounded token.
            sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
            keep = []
            for sent in sentences:
                if any(u in sent for u in ungrounded):
                    continue
                if sent.strip():
                    keep.append(sent.strip())
            stripped = " ".join(keep).strip()
            if not stripped:
                # All content was ungrounded numeric claims → REPLACE
                stripped = _pick_phrase(
                    "ungrounded_data", seed="|".join(ungrounded[:3]),
                )
                trace.update({
                    "status": "v2_replaced_ungrounded_data",
                    "v2_violations": ["ungrounded_numeric_data"],
                    "ungrounded_tokens": ungrounded[:5],
                    "reason": "all_numeric_tokens_ungrounded",
                    "__canonical_en": True,
                })
                logger.warning(
                    "honesty_layer_v2.replaced_ungrounded",
                    tokens=ungrounded[:5],
                )
                return stripped, trace
            else:
                trace.update({
                    "status": "v2_stripped_ungrounded_data",
                    "v2_violations": ["ungrounded_numeric_data"],
                    "ungrounded_tokens": ungrounded[:5],
                    "reason": "stripped_ungrounded_numeric_sentences",
                })
                logger.warning(
                    "honesty_layer_v2.stripped_ungrounded",
                    tokens=ungrounded[:5],
                    chars_removed=len(text) - len(stripped),
                )
                return stripped, trace

    # ── Check (c): Negative claim verification ────────────────────────────────
    # If the response says "I refused / could not" — was the operation
    # actually attempted and blocked? The honest framing is "I tried and was
    # blocked because <reason>", not "I refused" (which implies proactive
    # decision). Light touch: only annotate trace, don't rewrite, since the
    # LLM's phrasing is usually close enough.
    for pat in _NEGATIVE_CLAIM_PATTERNS:
        if pat.search(text):
            trace["v2_violations"].append("negative_claim_present")
            break
    # No automatic rewrite for negative claims — the existing canonical
    # blocked/failed templates cover the high-stakes cases (Path C above).

    return text, trace
