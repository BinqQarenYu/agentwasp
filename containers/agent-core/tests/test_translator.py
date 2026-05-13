"""Tests for communication.translator.

Uses a fake model_manager (no LLM call) and a mock Redis client (no real
Redis connection) so tests run anywhere. Real-world behaviour is verified
end-to-end via the multilingual batch in CI.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.communication.translator import (
    _normalise_lang,
    apick,
    translate,
)


class _FakeResponse:
    def __init__(self, content: str):
        self.content = content


class _FakeModelManager:
    """Returns a fixed response. Counts calls."""
    def __init__(self, content: str = "FAKE_TRANSLATION"):
        self.content = content
        self.calls = 0

    async def generate(self, request):
        self.calls += 1
        return _FakeResponse(self.content)


# ── Language normalisation ────────────────────────────────────────────────────

def test_normalise_lang_english_variants_short_circuit():
    for code in ("en", "EN", "en-US", "en-gb", "english", "ENG", ""):
        assert _normalise_lang(code) == ""


def test_normalise_lang_other_returns_lower():
    assert _normalise_lang("DE") == "de"
    assert _normalise_lang("Es-cl") == "es-cl"
    assert _normalise_lang("ja") == "ja"


# ── translate() short-circuit paths ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_translate_returns_text_for_english_target():
    """Target lang is English → no LLM call, no Redis."""
    mm = _FakeModelManager()
    out = await translate("hello world", "en", mm, redis_url=None)
    assert out == "hello world"
    assert mm.calls == 0


@pytest.mark.asyncio
async def test_translate_returns_text_for_empty_input():
    mm = _FakeModelManager()
    assert await translate("", "de", mm) == ""
    assert mm.calls == 0


@pytest.mark.asyncio
async def test_translate_fail_safe_when_no_model_manager():
    """No model manager → original English. Truth never broken by translation."""
    out = await translate("hello", "de", model_manager=None, redis_url=None)
    assert out == "hello"


@pytest.mark.asyncio
async def test_translate_fail_safe_on_llm_exception():
    class _BoomModel:
        async def generate(self, request):
            raise RuntimeError("provider down")
    out = await translate("hello", "de", _BoomModel(), redis_url=None)
    assert out == "hello"


@pytest.mark.asyncio
async def test_translate_calls_llm_for_non_english_target():
    mm = _FakeModelManager(content="hallo welt")
    out = await translate("hello world", "de", mm, redis_url=None)
    assert out == "hallo welt"
    assert mm.calls == 1


@pytest.mark.asyncio
async def test_translate_strips_quotes_and_preamble():
    mm = _FakeModelManager(content='Translation: "hallo welt"')
    out = await translate("hello world", "de", mm, redis_url=None)
    assert out == "hallo welt"


# ── apick() composition ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_apick_returns_canonical_when_lang_is_english():
    mm = _FakeModelManager()
    msg = await apick("url_required", "en", "seed1", mm, redis_url=None)
    assert "URL" in msg
    assert mm.calls == 0


@pytest.mark.asyncio
async def test_apick_calls_translator_for_other_lang():
    mm = _FakeModelManager(content="welche site")
    msg = await apick("url_required", "de", "seed1", mm, redis_url=None)
    assert msg == "welche site"
    assert mm.calls == 1


@pytest.mark.asyncio
async def test_apick_with_placeholders_substitutes_before_translation():
    """The English template is filled with kwargs BEFORE being sent to the
    LLM, so internal IDs (truth values, labels) reach the translator as
    real text. No raw {placeholder} should leak."""
    mm = _FakeModelManager(content="dein gespeicherter cat's name ist Pixel")
    msg = await apick(
        "attribute_truth_single", "de", "seed",
        mm, redis_url=None,
        label="cat's name", truth="Pixel",
    )
    assert "Pixel" in msg
    assert "{label}" not in msg
    assert "{truth}" not in msg


@pytest.mark.asyncio
async def test_apick_unknown_intent_returns_canonical_marker_for_english():
    """Unknown intent → canonical marker reaches publish (not crash)."""
    mm = _FakeModelManager()
    # English target → no translation, marker visible.
    msg = await apick("nonexistent_intent_zzz", "en", "seed", mm, redis_url=None)
    assert "missing intent" in msg.lower()


@pytest.mark.asyncio
async def test_apick_does_not_crash_on_unknown_intent_with_translation():
    """Unknown intent + non-English → translator still runs, no exception."""
    mm = _FakeModelManager(content="übersetzter platzhalter")
    msg = await apick("nonexistent_intent_zzz", "de", "seed", mm, redis_url=None)
    assert isinstance(msg, str) and msg.strip()
