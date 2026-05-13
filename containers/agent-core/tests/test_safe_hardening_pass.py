"""Tests for the safe hardening pass — covers B1, B5, B7, and B8 fixes.

Each test verifies a specific bug stays fixed without regressing the
truth-enforcement layer's existing behaviour.
"""
from __future__ import annotations

from src.communication.phrases import _CATALOGUE, pick
from src.events.control_layer import (
    _domain_matches_user,
    _extract_url_signals_from_user_text,
)
from src.events.response_binding import _check_memory_fabrication


# ── B1 — follow-up URL lock & substitution prevention ────────────────────────

def test_b1_followup_lock_phrase_present_in_catalogue():
    """The canonical refusal exists for both the no-context and the
    domain-mismatch paths."""
    assert "followup_no_context" in _CATALOGUE
    assert "domain_mismatch_block" in _CATALOGUE
    assert pick("followup_no_context", seed="x").strip()
    assert pick("domain_mismatch_block", seed="y").strip()


def test_b1_extract_url_signals_handles_no_url():
    """A follow-up message with no URL must produce empty sets so the
    caller knows to fall back to last_confirmed_domain."""
    well, bad = _extract_url_signals_from_user_text("haz lo mismo otra vez")
    assert not well
    assert not bad


def test_b1_domain_match_subdomains():
    assert _domain_matches_user("www.lasegunda.com", {"lasegunda.com"})
    assert _domain_matches_user("lasegunda.com", {"www.lasegunda.com"})
    assert not _domain_matches_user("coinbase.com", {"lasegunda.com"})


# ── B5 — email recipient fast-path ────────────────────────────────────────────

def test_b5_email_recipient_required_phrase_exists():
    assert "email_recipient_required" in _CATALOGUE
    msg = pick("email_recipient_required", seed="x")
    assert msg.strip()
    # Should be a question — asking for the address.
    assert any(t in msg.lower() for t in ("?", "address", "email"))


# ── B7 — memory fabrication guard ─────────────────────────────────────────────

class _R:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_b7_strips_fabricated_value_not_in_user_text_or_attributes():
    """Response says 'antes me dijiste turquesa, ahora dices rojo' but the
    user's current message is just 'turquesa'. 'rojo' is fabricated → strip."""
    stored = {"favourite_colour": "turquesa"}
    user_text = "Mi color favorito es turquesa"
    text = "Antes me dijiste turquesa, ahora dices rojo. ¿Cuál es correcto?"
    out = _check_memory_fabrication(text, user_text, stored, "es")
    assert out is not None
    cleaned, trace = out
    assert trace["status"] == "v2_memory_fabrication_stripped"
    # 'rojo' should be stripped — either gone entirely or replaced
    assert "rojo" not in cleaned.lower() or trace.get("__canonical_en") is True


def test_b7_passthrough_when_value_is_in_user_text():
    """If the user genuinely said 'rojo', the response can quote it."""
    stored = {"favourite_colour": "turquesa"}
    user_text = "Mi color favorito en realidad es rojo"
    text = "Antes me dijiste turquesa, ahora dices rojo. ¿Cuál es correcto?"
    out = _check_memory_fabrication(text, user_text, stored, "es")
    assert out is None  # no fabrication — both values are accounted for


def test_b7_passthrough_when_value_is_a_stored_attribute():
    stored = {"favourite_colour": "turquesa", "pet_cat": "Pixel"}
    user_text = "¿Cuál es mi color?"
    text = "Tu color favorito es turquesa."
    out = _check_memory_fabrication(text, user_text, stored, "es")
    assert out is None


# ── B8 — action announcer localised verbs ─────────────────────────────────────

def test_b8_action_announcer_localises_verbs():
    from src.policy.action_announcer import apply_action_announcer

    class _S:
        def __init__(self, name, ok=True, out=""):
            self.skill_name = name
            self.success = ok
            self.output = out
            self.error = None

    # ES path
    cleaned_es, _ = apply_action_announcer(
        "Te envié el reporte.",
        [_S("gmail", ok=True, out="ok, sent to alice@example.com")],
        user_lang="es",
    )
    assert "Acciones:" in cleaned_es
    assert "Correo enviado" in cleaned_es

    # EN path
    cleaned_en, _ = apply_action_announcer(
        "I sent the report.",
        [_S("gmail", ok=True, out="ok, sent to alice@example.com")],
        user_lang="en",
    )
    assert "Actions:" in cleaned_en
    assert "Email sent" in cleaned_en
