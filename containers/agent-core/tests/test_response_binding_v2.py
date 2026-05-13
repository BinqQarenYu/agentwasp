"""Tests for the v2 honesty layer additions:
- attribute truth override (DB > LLM)
- capability disclaimer override
- numeric data grounding
- canonical_en flag for downstream translation
"""
from __future__ import annotations

from src.events.response_binding import apply_honesty_layer


class _R:
    """Minimal SkillResult double."""
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


# ── Attribute truth override ──────────────────────────────────────────────────

def test_attribute_override_replaces_when_response_contradicts_db():
    stored = {"pet_cat": "Pixel", "favourite_colour": "turquoise"}
    text = "Your cat's name is Tigre."
    cleaned, trace = apply_honesty_layer(
        text, user_attributes=stored, user_lang="en",
    )
    assert "Pixel" in cleaned
    assert "Tigre" not in cleaned
    assert trace["status"] == "v2_attribute_truth_override"
    assert trace.get("__canonical_en") is True


def test_bare_value_mismatch_recap_lists_stored_attributes():
    stored = {"pet_cat": "Pixel", "favourite_colour": "turquoise"}
    text = "Red."  # doesn't match any stored value
    cleaned, trace = apply_honesty_layer(
        text, user_attributes=stored, user_lang="en",
    )
    assert "Pixel" in cleaned
    assert "turquoise" in cleaned
    assert trace.get("__canonical_en") is True


def test_passthrough_when_response_matches_stored_value():
    stored = {"pet_cat": "Pixel"}
    text = "Pixel."
    cleaned, trace = apply_honesty_layer(
        text, user_attributes=stored, user_lang="en",
    )
    assert cleaned == text
    assert trace["status"] == "passthrough"


def test_no_attributes_no_override():
    cleaned, trace = apply_honesty_layer(
        "I love coding.", user_attributes={}, user_lang="en",
    )
    assert cleaned == "I love coding."
    assert trace.get("__canonical_en") in (None, False)


# ── Capability disclaimer override ────────────────────────────────────────────

def test_capability_override_replaces_false_disclaimer_when_at_time_honored():
    """If task_manager succeeded with at_time set, the response can't claim
    'task_manager only supports intervals'."""
    skill_result = _R(
        skill_name="task_manager",
        success=True,
        output="Task scheduled. at_time: 8:00 AM. Next run 2026-05-06 08:00 hora chile",
    )
    text = (
        "Quedó programado. Nota: la tarea no se ejecuta a las 8 AM "
        "específicamente — task_manager solo soporta intervalos. "
        "Corre cada N horas desde el momento de creación."
    )
    cleaned, trace = apply_honesty_layer(
        text, skill_results=[skill_result], user_lang="es",
    )
    # Replacement is canonical English (translator localises post-honesty).
    assert "8" in cleaned  # the at_time evidence is preserved
    assert trace["status"] == "v2_capability_override"
    assert trace.get("__canonical_en") is True


# ── Numeric data grounding ────────────────────────────────────────────────────

def test_ungrounded_prices_replaced_with_canonical():
    """Response cites prices that no skill_result actually returned."""
    failed = _R(skill_name="browser", success=False, output="blocked", error="page blocked")
    text = "BTC: R$400,095.75  ETH: R$2,500.00"
    cleaned, trace = apply_honesty_layer(
        text, skill_results=[failed], user_lang="en",
    )
    assert "R$400,095.75" not in cleaned
    assert "R$2,500.00" not in cleaned
    assert trace["status"] == "v2_replaced_ungrounded_data"
    assert trace.get("__canonical_en") is True


def test_grounded_prices_pass_through():
    grounded = _R(
        skill_name="browser",
        success=True,
        output="BTC price is $48,237.50 USD as of now",
    )
    text = "BTC is $48,237.50 USD"
    cleaned, trace = apply_honesty_layer(
        text, skill_results=[grounded], user_lang="en",
    )
    assert "$48,237.50" in cleaned


def test_partial_ungrounded_strips_only_those_sentences():
    grounded = _R(
        skill_name="browser",
        success=True,
        output="ETH is $2,500 today",
    )
    text = "ETH is $2,500 today. BTC is $400,095.75 today."
    cleaned, trace = apply_honesty_layer(
        text, skill_results=[grounded], user_lang="en",
    )
    assert "$2,500" in cleaned
    assert "$400,095.75" not in cleaned
    assert trace["status"] == "v2_stripped_ungrounded_data"
