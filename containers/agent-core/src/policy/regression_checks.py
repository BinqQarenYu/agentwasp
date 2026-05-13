"""Deterministic prime.md regression checks.

Each check is a pure boolean over (user_text, args, ctx) tuples. Used by
tests/policy/test_regressions.py and importable from runtime if needed.

Goal: if a future change to prime.md (or a model swap) causes a regression
on a critical behavior, these checks fail loudly without requiring an
end-to-end LLM run.
"""
from __future__ import annotations

import re
from typing import Optional

from .intent_gate import (
    INTENT_GATE_PATTERNS,
    REFERENCE_PHRASE_RE,
    EMAIL_ADDR_RE,
    SIDE_EFFECT_SKILLS,
    SKILL_SAFE_ACTIONS,
    is_placeholder_subject,
    is_placeholder_body,
    user_message_provides_content,
)
from .response_guard import (
    TIME_CLAIM_RE,
    SCHEDULING_CONTEXT_RE,
    SIDE_EFFECT_ANNOUNCEMENT_PATTERNS,
)


# ── R1: gmail.send blocked without explicit email intent ────────────────


def check_email_blocked_without_intent(user_text: str) -> bool:
    """True iff user_text would fail the email intent gate
    (i.e. no explicit email keyword)."""
    return not bool(INTENT_GATE_PATTERNS["gmail"].search(user_text or ""))


# ── R2: schedule honesty — no fixed-time claim should pass ──────────────


def check_no_fixed_time_in_response(response_text: str) -> bool:
    """True iff response is honest about fixed-time scheduling.

    A response is dishonest if it makes a clock-time claim AND scheduling
    context is present AND the claim wasn't stripped by enforce_schedule_honesty.
    """
    if not response_text:
        return True
    if not TIME_CLAIM_RE.search(response_text):
        return True
    if not SCHEDULING_CONTEXT_RE.search(response_text):
        return True
    # Has clock time + scheduling context → dishonest unless explicit
    # "task_manager only supports interval" disclaimer is present.
    return bool(
        re.search(
            r"(only\s+supports\s+interval|solo\s+soporta\s+intervalos|"
            r"does\s+not\s+run\s+at\b|no\s+se\s+ejecuta\s+a\s+las)",
            response_text,
            re.IGNORECASE,
        )
    )


# ── R3: simple recurring tasks should NOT trigger sub-agent creation ────


def check_no_subagent_for_simple_recurring(user_text: str) -> bool:
    """True iff user_text is a simple recurring task that should NOT trigger
    agent_manager.create. Examples: "monitor BTC every hour", "envíame el
    precio diariamente" — these need task_manager only.
    """
    if not user_text:
        return True
    # If user explicitly said "agente" / "agent" / "sub-agent" → not simple
    if INTENT_GATE_PATTERNS["agent_manager"].search(user_text):
        return True  # explicit agent intent — not a regression target
    # Check if it has scheduling intent → must be deterministic recurring task
    return bool(INTENT_GATE_PATTERNS["task_manager"].search(user_text))


# ── R4: task instruction must be the user's verbatim message ────────────


def check_instruction_verbatim(user_text: str, task_instruction: str) -> bool:
    """True iff the task instruction is a verbatim copy of user_text (case-
    insensitive whitespace match), or contains it as a substring of ≥80% length.
    Used in tests + post-create assertion."""
    if not user_text or not task_instruction:
        return False
    norm_u = re.sub(r"\s+", " ", user_text.strip().lower())
    norm_t = re.sub(r"\s+", " ", task_instruction.strip().lower())
    if norm_u in norm_t:
        return True
    # 80% prefix match
    overlap = sum(1 for a, b in zip(norm_u, norm_t) if a == b)
    return overlap >= int(len(norm_u) * 0.8)


# ── R5: screenshot without URL must ask for URL ─────────────────────────


_SCREENSHOT_KEYWORDS_RE = re.compile(
    r"\b(captura(?:r|me)?|screenshot|pantalla(?:zo)?|"
    r"toma(?:r|me)?\s+(?:una\s+)?captura|capture(?:\s+me)?)\b",
    re.IGNORECASE,
)
_URL_PRESENT_RE = re.compile(r"\bhttps?://|www\.|\b[\w-]+\.(?:com|net|org|io|cl|es|mx|ar|co|pe)\b", re.IGNORECASE)


def check_screenshot_requires_url(user_text: str) -> tuple[bool, bool]:
    """Returns (is_screenshot_request, has_url).

    If is_screenshot_request and not has_url → response MUST ask for URL.
    """
    if not user_text:
        return False, False
    is_ss = bool(_SCREENSHOT_KEYWORDS_RE.search(user_text))
    has_url = bool(_URL_PRESENT_RE.search(user_text))
    return is_ss, has_url


# ── R6: gmail.send with placeholder content is blocked ──────────────────


def check_email_content_validity(args: dict, user_text: str) -> bool:
    """True iff gmail.send args have real, user-grounded content."""
    if not isinstance(args, dict):
        return False
    if user_message_provides_content(user_text or ""):
        return True
    subject = str(args.get("subject") or "")
    body = str(args.get("body") or "")
    if is_placeholder_subject(subject) or is_placeholder_body(body):
        return False
    # Args look real but user didn't provide content → still suspect (LLM
    # invention). Blocked by intent gate; here we surface that fact.
    return False


# ── R7: status questions must NOT trigger side-effects ──────────────────


_STATUS_QUESTION_RE = re.compile(
    r"^\s*(?:est[áa]s?\s+lista?|"
    r"(?:ya|terminaste|listo|done|ready)\??|"
    r"are\s+you\s+done|did\s+it\s+work|"
    r"qu[eé]\s+pas[oó]|c[oó]mo\s+va\??|"
    r"how\s+is\s+it\s+going|"
    r"todo\s+bien\??)\s*[?!.]?\s*$",
    re.IGNORECASE,
)


def check_status_question_no_side_effects(user_text: str) -> bool:
    """True iff text is a pure status question (must trigger no side-effects)."""
    return bool(_STATUS_QUESTION_RE.match((user_text or "").strip()))


# ── R8: vague request → must ask for clarification, not act ─────────────


_VAGUE_REQUEST_RE = re.compile(
    r"^\s*(?:haz\s+algo\s+(?:[úu]til|bueno|interesante)|"
    r"do\s+something\s+(?:useful|good|nice)|"
    r"sorpr[ée]ndeme|surprise\s+me|"
    r"ay[úu]dame|help\s+me)\s*[?!.]?\s*$",
    re.IGNORECASE,
)


def check_vague_request_asks_clarification(user_text: str) -> bool:
    """True iff the user's request is too vague to act on."""
    return bool(_VAGUE_REQUEST_RE.match((user_text or "").strip()))


# ── R9: hypothetical questions must NOT create tasks ────────────────────


_HYPOTHETICAL_RE = re.compile(
    r"\b(?:puedes\b|podr[íi]as|are\s+you\s+able|can\s+you\s+do|capaz\s+de)\b",
    re.IGNORECASE,
)


def check_hypothetical_no_task(user_text: str) -> bool:
    """True iff user_text is a hypothetical 'can you?' question without
    explicit scheduling keywords."""
    if not user_text:
        return False
    has_hypothetical = bool(_HYPOTHETICAL_RE.search(user_text))
    has_scheduling = bool(INTENT_GATE_PATTERNS["task_manager"].search(user_text))
    return has_hypothetical and not has_scheduling


# ── R10: retry confirmations are NOT new searches ──────────────────────


_RETRY_CONFIRM_RE = re.compile(
    r"^\s*(?:s[íi]|yes|ok|okay|okey|dale|listo|"
    r"int[ée]ntalo\s+de\s+nuevo|try\s+again|otra\s+vez|de\s+nuevo)\s*[?!.]?\s*$",
    re.IGNORECASE,
)


def check_retry_confirm_no_search(user_text: str) -> bool:
    """True iff user_text is a retry confirmation (must NOT trigger search)."""
    return bool(_RETRY_CONFIRM_RE.match((user_text or "").strip()))


# R11 removed: rule was specific to a hardcoded 17track URL format. The
# system no longer biases toward any single tracking aggregator — see
# `_plan_package_check` and `BrowserSkill.execute(action="track")`.


# ── R12: scheduling honesty disclaimer must mention "interval" ─────────


def check_schedule_disclaimer_mentions_interval(disclaimer_text: str) -> bool:
    """The schedule-honesty disclaimer must explain WHY (interval-only).
    Without this, users won't understand the limitation."""
    if not disclaimer_text:
        return False
    return bool(re.search(
        r"(only\s+supports\s+interval|solo\s+soporta\s+intervalos|"
        r"every\s+\d+\s+(?:hours?|days?|weeks?|months?)|"
        r"cada\s+\d+\s+(?:hora|d[íi]a|semana|mes))",
        disclaimer_text,
        re.IGNORECASE,
    ))


# ── R13: never echo internal /data/ paths to the user ──────────────────


_INTERNAL_PATH_RE = re.compile(r"/data/(?:chat-uploads|shared|screenshots?|memory|browser_sessions)/", re.IGNORECASE)


def check_no_internal_paths_in_response(response_text: str) -> bool:
    """True iff response is clean of internal /data/... paths.
    Screenshots are sent as photos; agent must not write the path in text."""
    if not response_text:
        return True
    return not bool(_INTERNAL_PATH_RE.search(response_text))


# ── R14: scheduled task triggers do NOT recreate the task ──────────────


_SCHEDULED_TRIGGER_PREFIX_RE = re.compile(r"^\[SCHEDULED TASK:\s*[^\]]+\]")


def check_scheduled_trigger_format(message_text: str) -> bool:
    """True iff a scheduled task message has the expected `[SCHEDULED TASK: …]`
    prefix. The handler routes by this prefix; missing it makes the agent
    treat it as a fresh user request and potentially create a duplicate."""
    return bool(_SCHEDULED_TRIGGER_PREFIX_RE.match((message_text or "").strip()))


# ── R15: response length discipline (one-sentence confirmations) ───────


def check_short_confirmation(response_text: str) -> bool:
    """True iff a Done/Listo confirmation is one or two sentences max.
    prime.md §9: 'Keep responses short. One sentence for confirmations.'"""
    if not response_text:
        return True
    # Count sentence terminators
    sentences = re.split(r"[.!?]+\s+", response_text.strip())
    sentences = [s for s in sentences if s.strip()]
    return len(sentences) <= 2


# ── R16: no markdown formatting leaks to Telegram ──────────────────────


_MD_BOLD_RE_CHECK = re.compile(r"\*\*[^*\n]+\*\*")
_MD_HEADER_RE_CHECK = re.compile(r"^#{1,4}\s+", re.MULTILINE)


def check_no_markdown_in_telegram_output(response_text: str) -> bool:
    """True iff response is free of markdown that doesn't render in Telegram."""
    if not response_text:
        return True
    if _MD_BOLD_RE_CHECK.search(response_text):
        return False
    if _MD_HEADER_RE_CHECK.search(response_text):
        return False
    return True


# ── R17: blocked source disclosure required ────────────────────────────


def check_blocked_source_disclosure(response_text: str, captures_blocked: list[str]) -> bool:
    """If captures_blocked is non-empty, the response MUST mention which
    sources were blocked. prime.md §6.1: 'state clearly: ... was blocked'."""
    if not captures_blocked:
        return True
    if not response_text:
        return False
    text_lower = response_text.lower()
    if not any(w in text_lower for w in ("blocked", "bloque", "no pude", "could not", "fail")):
        return False
    # At least one of the blocked sources must be named or "all" mentioned
    return any(src.lower() in text_lower for src in captures_blocked) or "all " in text_lower


# ── R18: vague replies must not invent placeholder values ──────────────


_PLACEHOLDER_TOKENS_IN_REPLY = re.compile(
    r"\b(TRACKINGCODE|YOUR_\w+|<[A-Z_]+>|XXX+|TODO|TBD|\[INSERT\b)\b",
    re.IGNORECASE,
)


def check_no_placeholder_in_reply(response_text: str) -> bool:
    """True iff response has no template-style placeholder tokens."""
    if not response_text:
        return True
    return not bool(_PLACEHOLDER_TOKENS_IN_REPLY.search(response_text))


# ── R19: status questions get plain text, not skill calls ──────────────


def check_status_q_no_skill_calls(response_text: str) -> bool:
    """True iff a status-question response does not contain raw skill call
    blocks (which would mean the agent invoked something instead of just
    replying)."""
    if not response_text:
        return True
    return "<skill>" not in response_text.lower() and "<parallel>" not in response_text.lower()


# ── R20: reminder deletion requires the skill to actually run ──────────


def check_reminder_delete_was_real(response_text: str, delete_skill_ran: bool) -> bool:
    """If response claims a reminder was deleted, the skill must have run.
    prime.md §3.3: 'Never claim a reminder is deleted without calling the skill.'"""
    if not response_text:
        return True
    claims_delete = bool(re.search(
        r"(reminder\s+deleted|recordatorio\s+eliminad[ao]|deleted\s+the\s+reminder|"
        r"borr[éeo]\s+(?:el\s+)?recordatorio)",
        response_text, re.IGNORECASE,
    ))
    if not claims_delete:
        return True  # No claim → no contradiction possible
    return delete_skill_ran


# ── Aggregate runner — used by tests/policy/test_regressions.py ─────────


REGRESSION_CASES = [
    # (id, user_text, callable, expected, description)
    (
        "R1.email_no_intent_blocked",
        "captura google.com",
        lambda t: check_email_blocked_without_intent(t),
        True,
        "Pure screenshot request must NOT pass email intent gate.",
    ),
    (
        "R1.email_with_intent_passes",
        "envía a alice@example.com el resumen",
        lambda t: check_email_blocked_without_intent(t),
        False,
        "Explicit 'envía' must pass email intent gate.",
    ),
    (
        "R3.recurring_no_subagent",
        "monitorea BTC cada hora y mándame el precio",
        lambda t: check_no_subagent_for_simple_recurring(t),
        True,
        "Simple recurring task should be task_manager, not agent.",
    ),
    (
        "R5.screenshot_no_url",
        "tómame un screenshot",
        lambda t: check_screenshot_requires_url(t),
        (True, False),
        "Screenshot request without URL must be detected.",
    ),
    (
        "R5.screenshot_with_url",
        "captura https://example.com",
        lambda t: check_screenshot_requires_url(t),
        (True, True),
        "Screenshot with URL passes.",
    ),
    (
        "R7.status_no_side_effect",
        "todo bien?",
        lambda t: check_status_question_no_side_effects(t),
        True,
        "Status question must not trigger any side-effect.",
    ),
    (
        "R8.vague_asks_clarification",
        "haz algo útil",
        lambda t: check_vague_request_asks_clarification(t),
        True,
        "Vague request must trigger clarification, not action.",
    ),
    (
        "R9.hypothetical_no_task",
        "puedes monitorear bitcoin?",
        lambda t: check_hypothetical_no_task(t),
        True,
        "Hypothetical 'can you?' question must not create a task.",
    ),
    (
        "R10.retry_confirm_yes",
        "sí",
        lambda t: check_retry_confirm_no_search(t),
        True,
        "'sí' is a retry confirmation, not a new search query.",
    ),
    (
        "R10.retry_confirm_dale",
        "dale",
        lambda t: check_retry_confirm_no_search(t),
        True,
        "'dale' is a retry confirmation.",
    ),
    (
        "R10.not_retry",
        "captura google.com",
        lambda t: check_retry_confirm_no_search(t),
        False,
        "Real request must not be classified as retry confirm.",
    ),
    (
        "R12.disclaimer_explains_interval",
        "I cannot schedule at 8 am — task_manager only supports interval scheduling.",
        lambda t: check_schedule_disclaimer_mentions_interval(t),
        True,
        "Schedule-honesty disclaimer must explain WHY (interval-only).",
    ),
    (
        "R12.disclaimer_missing_explanation",
        "Sorry, I cannot do that.",
        lambda t: check_schedule_disclaimer_mentions_interval(t),
        False,
        "Generic refusal without 'interval' keyword fails the rule.",
    ),
    (
        "R13.no_internal_path_in_response",
        "Aquí está el screenshot que pediste.",
        lambda t: check_no_internal_paths_in_response(t),
        True,
        "Clean response — no /data/ path leakage.",
    ),
    (
        "R13.internal_path_leak",
        "Saved to /data/screenshots/screenshot_123.png",
        lambda t: check_no_internal_paths_in_response(t),
        False,
        "Internal path /data/screenshots/... must not appear in user-facing text.",
    ),
    (
        "R14.scheduled_trigger_format",
        "[SCHEDULED TASK: daily AI news]\nrun the report",
        lambda t: check_scheduled_trigger_format(t),
        True,
        "Scheduled trigger has expected prefix.",
    ),
    (
        "R14.no_prefix",
        "run the report now",
        lambda t: check_scheduled_trigger_format(t),
        False,
        "Plain message has no scheduled-task prefix.",
    ),
    (
        "R15.short_confirmation_ok",
        "Done. Email sent to alice@example.com.",
        lambda t: check_short_confirmation(t),
        True,
        "Two-sentence confirmation is allowed.",
    ),
    (
        "R15.too_long_confirmation",
        "Done. I sent the email. I scheduled the task. I created the agent. I notified you.",
        lambda t: check_short_confirmation(t),
        False,
        "Five-sentence confirmation violates output-format discipline.",
    ),
    (
        "R16.markdown_bold_leak",
        "**Important update**: the report is ready.",
        lambda t: check_no_markdown_in_telegram_output(t),
        False,
        "Markdown bold must not leak to Telegram.",
    ),
    (
        "R16.markdown_clean",
        "The report is ready.",
        lambda t: check_no_markdown_in_telegram_output(t),
        True,
        "Plain text passes.",
    ),
    (
        "R18.placeholder_in_reply",
        "Your tracking code is TRACKINGCODE and your name is YOUR_NAME.",
        lambda t: check_no_placeholder_in_reply(t),
        False,
        "Placeholder tokens like TRACKINGCODE / YOUR_NAME must never appear in replies.",
    ),
    (
        "R18.real_values",
        "Your tracking code is XQ123456789CN and your name is Juan.",
        lambda t: check_no_placeholder_in_reply(t),
        True,
        "Real values are fine.",
    ),
    (
        "R19.status_q_no_skills",
        "Todo en orden, gracias.",
        lambda t: check_status_q_no_skill_calls(t),
        True,
        "Status reply has no skill blocks.",
    ),
    (
        "R19.skill_call_in_reply",
        "Voy a revisar. <skill>browser action=capture</skill>",
        lambda t: check_status_q_no_skill_calls(t),
        False,
        "Skill block leaked into user reply text.",
    ),
]


def run_all() -> tuple[int, int, list[str]]:
    """Run every regression case. Returns (passed, total, failures)."""
    passed = 0
    failures: list[str] = []
    for case_id, user_text, fn, expected, desc in REGRESSION_CASES:
        try:
            actual = fn(user_text)
            if actual == expected:
                passed += 1
            else:
                failures.append(f"{case_id}: expected={expected!r} actual={actual!r} — {desc}")
        except Exception as e:
            failures.append(f"{case_id}: ERROR {e!r} — {desc}")
    return passed, len(REGRESSION_CASES), failures
