"""Deterministic regression checks for the central policy enforcement.

These tests pin down behaviors that the prime.md operational directives
promise. If a future change to a regex, prime.md, or the model swap makes
any of these fail, that is a regression — fix the gate, not the test.

Each test runs in isolation against pure functions. No I/O, no LLM, no
Redis. The whole suite finishes in milliseconds.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import pytest

from src.policy import (
    INTENT_GATE_PATTERNS,
    REFERENCE_PHRASE_RE,
    intent_gate_check,
    filter_inferred_side_effects,
    enforce_schedule_honesty,
    enforce_side_effect_text_gate,
    apply_final_response_policy,
    is_placeholder_subject,
    is_placeholder_body,
    user_message_provides_content,
)
from src.policy.regression_checks import (
    REGRESSION_CASES,
    run_all,
    check_email_blocked_without_intent,
    check_no_fixed_time_in_response,
    check_no_subagent_for_simple_recurring,
    check_instruction_verbatim,
    check_screenshot_requires_url,
    check_email_content_validity,
    check_status_question_no_side_effects,
    check_vague_request_asks_clarification,
    check_hypothetical_no_task,
)


# ── Test fixtures ────────────────────────────────────────────────────────


@dataclass
class FakeSkillCall:
    skill_name: str
    arguments: dict


@dataclass
class FakeSkillResult:
    skill_name: str
    success: bool = True
    output: str = ""
    error: str = ""


# ── Aggregate regression suite ───────────────────────────────────────────


def test_regression_suite_all_pass():
    passed, total, failures = run_all()
    assert not failures, f"Regression failures:\n  " + "\n  ".join(failures)
    assert passed == total


# ── Intent gate ──────────────────────────────────────────────────────────


def test_email_blocked_without_intent():
    """Pure screenshot request must NEVER pass the email gate."""
    sc = FakeSkillCall(skill_name="gmail", arguments={"action": "send",
                                                       "to": "alice@example.com",
                                                       "subject": "Hi",
                                                       "body": "Body content here that is long enough."})
    ok, reason, label = intent_gate_check(sc, "captura google.com")
    assert not ok, "gmail.send must be blocked when user only asked for a screenshot"
    assert reason == "no_explicit_intent_in_user_message"
    assert label == "inferred_blocked"


def test_email_allowed_with_explicit_intent_and_recipient():
    sc = FakeSkillCall(
        skill_name="gmail",
        arguments={"action": "send", "to": "alice@example.com",
                   "subject": "Resumen", "body": "Aquí está el resumen completo."},
    )
    user = "envía a alice@example.com el resumen del día con detalles"
    ok, reason, label = intent_gate_check(sc, user)
    assert ok, f"explicit email request must pass — got {reason}"


def test_email_blocked_when_recipient_invented():
    """Recipient in args but never appeared in user message → block."""
    sc = FakeSkillCall(
        skill_name="gmail",
        arguments={"action": "send", "to": "unauthorized@unknown.example",
                   "subject": "Resumen", "body": "Lorem ipsum dolor sit amet."},
    )
    user = "envíame el resumen"  # no email address provided
    ok, reason, _ = intent_gate_check(sc, user)
    assert not ok
    assert reason == "missing_recipient"


def test_agent_create_blocked_for_simple_recurring():
    """Recurring task must NOT trigger sub-agent unless user said 'agente'."""
    sc = FakeSkillCall(skill_name="agent_manager", arguments={"action": "create"})
    user = "monitorea BTC cada hora y mándame el precio"
    ok, reason, _ = intent_gate_check(sc, user)
    assert not ok, "agent_manager.create must require explicit 'agente' keyword"


def test_agent_create_allowed_when_explicit():
    sc = FakeSkillCall(skill_name="agent_manager", arguments={"action": "create"})
    user = "crea un agente que monitoree el dólar"
    ok, _, _ = intent_gate_check(sc, user)
    assert ok


def test_agent_create_allowed_with_pronouns_and_missing_articles():
    sc = FakeSkillCall(skill_name="agent_manager", arguments={"action": "create"})
    # English with pronoun in middle and missing article
    user1 = "Create me Agent specialized in managing my gmail read it and knows what it is"
    ok1, _, _ = intent_gate_check(sc, user1)
    assert ok1

    # Spanish with pronoun and missing article
    user2 = "creame agente para enviar correos"
    ok2, _, _ = intent_gate_check(sc, user2)
    assert ok2

    # English with provide verb
    user3 = "provide me email agent specialized in managing email"
    ok3, _, _ = intent_gate_check(sc, user3)
    assert ok3

    # English with give verb
    user4 = "give me agent for scraping"
    ok4, _, _ = intent_gate_check(sc, user4)
    assert ok4


def test_task_create_allowed_with_recurring_keyword():
    sc = FakeSkillCall(skill_name="task_manager", arguments={"action": "create"})
    ok, _, _ = intent_gate_check(sc, "envíame el precio del bitcoin cada hora")
    assert ok


def test_safe_actions_bypass_gate():
    for action in ("list", "trigger", "delete", "status"):
        sc = FakeSkillCall(skill_name="task_manager", arguments={"action": action})
        ok, _, label = intent_gate_check(sc, "hola")
        assert ok, f"task_manager.{action} should be safe"
        assert label == "non_side_effect"


def test_few_shot_messages_do_not_authorize_email():
    """A few-shot example with role=user containing 'envíame el reporte' must
    NOT count as real user intent for gmail.send."""
    class FakeMsg:
        def __init__(self, role, content, meta=None):
            self.role = role
            self.content = content
            self.meta = meta or {}

    fewshots = [
        FakeMsg("user",      "envíame el reporte completo al correo user@example.com.",
                meta={"fewshot": True}),
        FakeMsg("assistant", "<skill>gmail send to=user@example.com</skill>",
                meta={"fewshot": True}),
    ]
    sc = FakeSkillCall(skill_name="gmail", arguments={"action": "send",
                                                       "to": "user@example.com",
                                                       "subject": "X", "body": "lorem ipsum dolor longer body"})
    # Current message is a screenshot request — no email intent
    ok, reason, _ = intent_gate_check(sc, "captura google.com", ctx_messages=fewshots)
    assert not ok, "few-shot examples must not authorize a real email send"
    assert reason == "no_explicit_intent_in_user_message"


def test_filter_drops_inferred_calls():
    calls = [
        FakeSkillCall(skill_name="browser", arguments={"action": "capture", "url": "https://x.com"}),
        FakeSkillCall(skill_name="gmail", arguments={"action": "send", "to": "alice@example.com",
                                                     "subject": "Hi", "body": "a longer body content here"}),
    ]
    allowed, dropped = filter_inferred_side_effects(calls, "captura https://x.com")
    assert len(allowed) == 1 and allowed[0].skill_name == "browser"
    assert len(dropped) == 1 and dropped[0][0].skill_name == "gmail"


# ── Schedule honesty ────────────────────────────────────────────────────


def test_schedule_honesty_strips_clock_time_with_real_create():
    res = [FakeSkillResult(skill_name="task_manager",
                           output="Task created: daily AI news report")]
    text = "Listo, programé la tarea diaria a las 8 am."
    cleaned, trace = enforce_schedule_honesty(text, res, user_lang="es")
    # The literal claim "a las 8 am" is stripped from the body but the
    # honest disclaimer references the time deliberately.
    assert "programé la tarea diaria a las 8 am" not in cleaned.lower()
    assert trace["applied"]
    assert trace["had_real_create"]
    assert "no se ejecuta a las 8 am" in cleaned


def test_schedule_honesty_strips_clock_time_without_real_create():
    text = "Programé la tarea para las 9 am."
    cleaned, trace = enforce_schedule_honesty(text, [], user_lang="en")
    assert trace["applied"]
    assert not trace["had_real_create"]
    # Body is stripped; disclaimer mentions the requested time.
    assert "Programé la tarea para las 9 am" not in cleaned
    assert "I cannot schedule at 9 am" in cleaned


def test_schedule_honesty_noop_when_no_time_claim():
    text = "Done. The screenshot is attached."
    cleaned, trace = enforce_schedule_honesty(text, [], user_lang="en")
    assert cleaned == text
    assert not trace["applied"]


def test_schedule_honesty_noop_outside_scheduling_context():
    text = "I checked at 8 am and the price was 50000."
    cleaned, trace = enforce_schedule_honesty(text, [], user_lang="en")
    assert cleaned == text
    assert not trace["applied"]


# ── Side-effect text gate ───────────────────────────────────────────────


def test_text_gate_strips_email_announcement_when_unauthorized():
    text = "Te envío el reporte por correo."
    cleaned, trace = enforce_side_effect_text_gate(
        text, "captura google.com", [], user_lang="es",
    )
    assert "email" in trace["rewrites"]
    assert "envío el reporte por correo" not in cleaned


def test_text_gate_keeps_announcement_when_email_actually_ran():
    res = [FakeSkillResult(skill_name="gmail", output="ok")]
    text = "Te envié el reporte por correo."
    cleaned, trace = enforce_side_effect_text_gate(
        text, "envíame el reporte", res, user_lang="es",
    )
    # gmail ran successfully → announcement is honest, no rewrite
    assert "email" not in trace["rewrites"]


def test_text_gate_strips_task_creation_lie():
    text = "He programado la tarea diaria."
    cleaned, trace = enforce_side_effect_text_gate(
        text, "captura google.com", [], user_lang="es",
    )
    assert "task_create" in trace["rewrites"]


# ── Final-response policy (composite) ───────────────────────────────────


def test_final_policy_composes_both_guards():
    """End-to-end: policy strips clock-time AND unauthorized email claim."""
    text = "Programé la tarea diaria a las 8 am y te envío el reporte por correo."
    cleaned, trace = apply_final_response_policy(
        text,
        user_text="captura google.com",
        skill_results=[],
        user_lang="es",
        chat_id="t1",
    )
    assert trace["schedule_honesty"]["applied"]
    assert "email" in trace["side_effect_text"]["rewrites"]
    # Lying body removed (the original "a las 8 am" claim is gone from main text).
    assert "Programé la tarea diaria a las 8 am" not in cleaned
    # Unauthorized email announcement scrubbed.
    assert "envío el reporte por correo" not in cleaned
    # Honest disclaimer present.
    assert "no puedo programar a las 8 am" in cleaned


# ── Placeholder content detection ───────────────────────────────────────


@pytest.mark.parametrize("subject,is_ph", [
    ("", True),
    ("subject", True),
    ("Your Subject Here", True),
    ("Saludos", True),                   # short greeting → placeholder
    ("Daily AI news summary 2026-04-29", False),
])
def test_placeholder_subject_detection(subject, is_ph):
    assert is_placeholder_subject(subject) is is_ph


@pytest.mark.parametrize("body,is_ph", [
    ("", True),
    ("hello", True),
    ("Saludos", True),
    ("Aquí está el reporte completo del día con todas las cifras solicitadas.", False),
])
def test_placeholder_body_detection(body, is_ph):
    assert is_placeholder_body(body) is is_ph


# ── Prime.md regression checks (one-off helper functions) ──────────────


def test_screenshot_requires_url_check():
    is_ss, has_url = check_screenshot_requires_url("tómame un screenshot")
    assert is_ss and not has_url
    is_ss, has_url = check_screenshot_requires_url("captura https://example.com")
    assert is_ss and has_url


def test_status_question_check():
    assert check_status_question_no_side_effects("todo bien?")
    assert check_status_question_no_side_effects("ya?")
    assert not check_status_question_no_side_effects("envíame el reporte")


def test_vague_request_check():
    assert check_vague_request_asks_clarification("haz algo útil")
    assert check_vague_request_asks_clarification("sorpréndeme")
    assert not check_vague_request_asks_clarification("captura google.com")


def test_hypothetical_no_task_check():
    assert check_hypothetical_no_task("puedes monitorear bitcoin?")
    assert not check_hypothetical_no_task("monitorea bitcoin cada hora")


# ── Action announcer ───────────────────────────────────────────────────

from src.policy import (
    apply_action_announcer,
    collect_actions,
    render_actions_block,
    strip_unverified_claims,
)


def test_announcer_strips_unverified_email_claim():
    """LLM said 'enviaré el reporte' but no gmail call ran → strip + no block."""
    text = "Aquí está el resumen. Te enviaré el reporte por correo en un momento."
    cleaned, trace = apply_action_announcer(text, [], user_lang="es")
    assert "email_send" in trace["stripped_families"]
    assert "enviar" not in cleaned.lower()
    assert "Acciones:" not in cleaned  # no block when no successful action
    assert "Aquí está el resumen" in cleaned


def test_announcer_keeps_verified_claim_and_renders_block():
    """gmail.send succeeded → claim survives + structured block appended."""
    res = [FakeSkillResult(skill_name="gmail", output="ok, sent to alice@example.com")]
    text = "Te envié el reporte."
    cleaned, trace = apply_action_announcer(text, res, user_lang="es")
    assert trace["stripped_families"] == []
    assert "email_send" in trace["actions_rendered"]
    assert "Acciones:" in cleaned
    # B8 fix: action verbs are localised. ES → "Correo enviado".
    assert "Correo enviado a alice@example.com" in cleaned


def test_announcer_strips_future_task_claim_when_no_create():
    """Future-tense lie when no real task_manager.create happened."""
    text = "Voy a programar la tarea diaria. Te aviso cada día."
    cleaned, trace = apply_action_announcer(text, [], user_lang="es")
    assert "task_create" in trace["stripped_families"]
    assert "Voy a programar la tarea" not in cleaned
    # Non-action sentence preserved
    assert "Te aviso" in cleaned


def test_announcer_renders_block_for_real_task_create():
    res = [FakeSkillResult(skill_name="task_manager",
                           output="Task created: daily AI news report")]
    text = "Done."
    cleaned, trace = apply_action_announcer(text, res, user_lang="en")
    assert "task_create" in trace["actions_rendered"]
    assert "Actions:" in cleaned
    assert "daily AI news report" in cleaned


def test_announcer_handles_failed_action():
    """Failed gmail call → block shows failed status, no claim survives."""
    res = [FakeSkillResult(skill_name="gmail", success=False,
                           output="", error="SMTP auth failed")]
    text = "I will send the report."
    cleaned, trace = apply_action_announcer(text, res, user_lang="en")
    # Claim stripped (skill didn't succeed)
    assert "email_send" in trace["stripped_families"]
    # Failed action shows up in block (operator visibility)
    assert "email_send failed" in cleaned
    assert "SMTP auth failed" in cleaned


def test_announcer_no_block_when_no_actions():
    """Pure Q&A response with no side-effects → no block appended."""
    text = "The current price is $50,000."
    cleaned, trace = apply_action_announcer(text, [], user_lang="en")
    assert trace["stripped_families"] == []
    assert trace["actions_rendered"] == []
    assert cleaned == text


def test_final_policy_uses_announcer():
    """End-to-end: announcer catches a claim that side_effect_text doesn't.

    The side_effect_text pattern requires an explicit noun after "email"
    ("send the report", "email the summary"). The announcer's pattern is
    broader — it catches "I'll email you tomorrow" without a noun. This
    test pins down the second-layer coverage.
    """
    text = "Here is the data. I'll email you tomorrow."
    cleaned, trace = apply_final_response_policy(
        text,
        user_text="show me the data",
        skill_results=[],
        user_lang="en",
        chat_id="t1",
    )
    assert "action_announcer" in trace
    assert "email_send" in trace["action_announcer"]["stripped_families"]
    assert "I'll email you tomorrow" not in cleaned


# ── Language consistency ───────────────────────────────────────────────


def test_language_consistency_translates_es_to_en():
    """Skill output leaked Spanish weekday/month → policy translates for EN user."""
    text = "Today is domingo, 29 de abril 2026, at 14:30."
    cleaned, trace = apply_final_response_policy(
        text,
        user_text="What's the date?",
        skill_results=[],
        user_lang="en",
        chat_id="t1",
    )
    assert trace["language_consistency"]["applied"]
    assert "Sunday" in cleaned
    assert "April" in cleaned
    assert "domingo" not in cleaned.lower()


def test_language_consistency_es_user_unchanged():
    """ES user gets ES text unchanged."""
    text = "Hoy es domingo, 29 de abril."
    cleaned, trace = apply_final_response_policy(
        text,
        user_text="qué día es hoy?",
        skill_results=[],
        user_lang="es",
        chat_id="t1",
    )
    assert not trace["language_consistency"]["applied"]
    assert cleaned == text


def test_language_consistency_translates_en_to_es():
    """LLM leaked EN weekday/month into mostly-Spanish text → translate for ES user."""
    text = "La hora actual es 1:25 PM del Wednesday 29 de April de 2026."
    cleaned, trace = apply_final_response_policy(
        text,
        user_text="qué hora es?",
        skill_results=[],
        user_lang="es",
        chat_id="t1",
    )
    assert trace["language_consistency"]["applied"]
    assert "miércoles" in cleaned.lower()
    assert "abril" in cleaned.lower()
    assert "wednesday" not in cleaned.lower()
    assert "april" not in cleaned.lower()
