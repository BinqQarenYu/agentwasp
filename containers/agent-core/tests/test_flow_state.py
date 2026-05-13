"""Tests for active flow context continuity (flow_state.py).

Covers the critical scenario: crypto report fails → user follow-up →
system must stay anchored to crypto domain, not drift to weather or other domains.
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.events.flow_state import (
    build_flow_context_block,
    detect_flow_assets,
    is_crypto_recovery_followup,
    is_explicit_domain_switch,
)


# ── Fixtures ───────────────────────────────────────────────────────────────────

CRYPTO_FLOW = {
    "domain": "crypto",
    "flow_type": "CRYPTO_REPORT",
    "assets": ["BTC", "ETH", "SOL"],
    "delivery": ["email"],
    "last_failure": "Pasos faltantes: render_report, gmail",
    "instruction": "[TAREA PROGRAMADA: abc] Informe de BTC/ETH/SOL",
    "stored_at": time.time(),
}


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario A — Follow-up stays in crypto flow
# ═══════════════════════════════════════════════════════════════════════════════

class TestCryptoFollowupDetection:
    def test_source_change_coinbase(self):
        """'puedes buscar la informacion en coinbase' → crypto recovery."""
        assert is_crypto_recovery_followup(
            "si, puedes buscar la informacion en otro lado pero idealmente coinbase, "
            "coinmarketcap o coingecko"
        )

    def test_source_change_coingecko(self):
        assert is_crypto_recovery_followup("usa coingecko")

    def test_source_change_coinmarketcap(self):
        assert is_crypto_recovery_followup("intenta con coinmarketcap")

    def test_try_another_source(self):
        assert is_crypto_recovery_followup("busca en otra fuente")

    def test_si_puedes_buscar(self):
        assert is_crypto_recovery_followup("si, puedes buscar ahí")

    def test_prueba_con_otra_api(self):
        assert is_crypto_recovery_followup("prueba con otra api")

    def test_cambia_la_fuente(self):
        assert is_crypto_recovery_followup("cambia la fuente de datos")

    def test_claro_usa(self):
        assert is_crypto_recovery_followup("claro, usa binance")


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario B — Source update → stay in crypto flow
# ═══════════════════════════════════════════════════════════════════════════════

class TestStayInCryptoFlow:
    def test_usa_coingecko_not_domain_switch(self):
        """'usa coingecko' should NOT trigger domain switch."""
        assert not is_explicit_domain_switch("usa coingecko", CRYPTO_FLOW)

    def test_busca_en_coinbase_not_domain_switch(self):
        assert not is_explicit_domain_switch("busca en coinbase", CRYPTO_FLOW)

    def test_intenta_de_nuevo_not_domain_switch(self):
        assert not is_explicit_domain_switch("intenta de nuevo", CRYPTO_FLOW)

    def test_si_puedes_buscar_not_domain_switch(self):
        msg = "si, puedes buscar la informacion en otro lado pero idealmente coinbase"
        assert not is_explicit_domain_switch(msg, CRYPTO_FLOW)

    def test_bare_affirmative_not_domain_switch(self):
        assert not is_explicit_domain_switch("sí, claro", CRYPTO_FLOW)


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario C — Explicit domain switch allowed
# ═══════════════════════════════════════════════════════════════════════════════

class TestExplicitDomainSwitch:
    def test_olvida_eso_clima(self):
        """'olvida eso, dime el clima en Londres' → domain switch."""
        assert is_explicit_domain_switch(
            "olvida eso, dime el clima en Londres", CRYPTO_FLOW
        )

    def test_forget_it_weather(self):
        assert is_explicit_domain_switch(
            "forget it, what's the weather in New York", CRYPTO_FLOW
        )

    def test_cancela_el_informe(self):
        """Cancela alone (no new domain mentioned) should NOT be a switch."""
        # "cancela" without a new unrelated topic is ambiguous — we allow the flow to continue
        result = is_explicit_domain_switch("cancela el informe", CRYPTO_FLOW)
        # We expect False here because there's no explicit unrelated domain
        assert not result

    def test_nueva_pregunta_with_weather(self):
        assert is_explicit_domain_switch(
            "nueva pregunta: ¿cuál es el clima en Lima?", CRYPTO_FLOW
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario D — Weather after crypto failure → domain NOT switched without explicit signal
# ═══════════════════════════════════════════════════════════════════════════════

class TestDomainContaminationGuard:
    def test_sigues_alucinando_clima_not_switch(self):
        """'sigues alucinando con el clima' is a COMPLAINT, not a domain switch."""
        msg = "sigues alucinando con el clima"
        # Should not be detected as explicit domain switch
        assert not is_explicit_domain_switch(msg, CRYPTO_FLOW)

    def test_por_que_dices_clima_not_switch(self):
        """User correcting a wrong answer shouldn't be treated as topic change."""
        msg = "por qué me hablas del clima? quiero el informe de BTC"
        assert not is_explicit_domain_switch(msg, CRYPTO_FLOW)

    def test_weather_correction_stays_in_crypto(self):
        """After a weather hallucination, user correction keeps crypto flow."""
        msg = "no me interesa el clima, necesito el informe de cripto"
        assert not is_explicit_domain_switch(msg, CRYPTO_FLOW)


# ═══════════════════════════════════════════════════════════════════════════════
# Asset detection
# ═══════════════════════════════════════════════════════════════════════════════

class TestDetectFlowAssets:
    def test_detect_btc_eth_sol(self):
        text = "Informe de BTC, ETH y SOL cada 2 minutos"
        assets = detect_flow_assets(text)
        assert "BTC" in assets
        assert "ETH" in assets
        assert "SOL" in assets

    def test_detect_bitcoin_ethereum(self):
        assets = detect_flow_assets("bitcoin and ethereum report")
        assert "BTC" in assets
        assert "ETH" in assets

    def test_no_duplicates(self):
        assets = detect_flow_assets("BTC btc bitcoin")
        assert assets.count("BTC") == 1

    def test_empty_text(self):
        assert detect_flow_assets("dime el clima") == []


# ═══════════════════════════════════════════════════════════════════════════════
# Context block generation
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildFlowContextBlock:
    def test_block_contains_domain(self):
        block = build_flow_context_block(CRYPTO_FLOW)
        assert "CRYPTO" in block

    def test_block_contains_assets(self):
        block = build_flow_context_block(CRYPTO_FLOW)
        assert "BTC" in block
        assert "ETH" in block

    def test_block_contains_no_weather_rule(self):
        block = build_flow_context_block(CRYPTO_FLOW)
        assert "weather" in block.lower() or "clima" in block.lower() or "unrelated" in block.lower()

    def test_block_contains_mandatory_rules(self):
        block = build_flow_context_block(CRYPTO_FLOW)
        assert "MANDATORY" in block or "RULE" in block

    def test_block_contains_failure_reason(self):
        block = build_flow_context_block(CRYPTO_FLOW)
        assert "render_report" in block or "Pasos faltantes" in block

    def test_block_contains_safe_fallback_phrase(self):
        block = build_flow_context_block(CRYPTO_FLOW)
        assert "CoinGecko" in block or "coingecko" in block.lower()


# ═══════════════════════════════════════════════════════════════════════════════
# Redis storage (mocked)
# ═══════════════════════════════════════════════════════════════════════════════

class TestRedisStorage:
    @pytest.mark.asyncio
    async def test_save_and_load(self):
        """save_active_flow then load_active_flow returns same data."""
        stored: dict[str, str] = {}

        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(side_effect=lambda k, v, ex=None: stored.__setitem__(k, v))
        mock_redis.get = AsyncMock(side_effect=lambda k: stored.get(k))
        mock_redis.aclose = AsyncMock()

        with patch("src.events.flow_state.aioredis.from_url", return_value=mock_redis):
            from src.events.flow_state import save_active_flow, load_active_flow

            await save_active_flow("redis://localhost", "12345", {
                "domain": "crypto",
                "flow_type": "CRYPTO_REPORT",
                "assets": ["BTC"],
                "delivery": ["email"],
                "last_failure": "missing gmail",
                "instruction": "test",
            })
            result = await load_active_flow("redis://localhost", "12345")

        assert result is not None
        assert result["domain"] == "crypto"
        assert result["flow_type"] == "CRYPTO_REPORT"
        assert "BTC" in result["assets"]

    @pytest.mark.asyncio
    async def test_clear_removes_key(self):
        stored: dict[str, str] = {"active_flow:99999": '{"domain":"crypto"}'}

        mock_redis = AsyncMock()
        mock_redis.delete = AsyncMock(side_effect=lambda k: stored.pop(k, None) and 1 or 0)
        mock_redis.get = AsyncMock(side_effect=lambda k: stored.get(k))
        mock_redis.aclose = AsyncMock()

        with patch("src.events.flow_state.aioredis.from_url", return_value=mock_redis):
            from src.events.flow_state import clear_active_flow, load_active_flow

            await clear_active_flow("redis://localhost", "99999")
            result = await load_active_flow("redis://localhost", "99999")

        assert result is None

    @pytest.mark.asyncio
    async def test_no_redis_url_returns_none(self):
        from src.events.flow_state import load_active_flow
        result = await load_active_flow("", "12345")
        assert result is None

    @pytest.mark.asyncio
    async def test_missing_key_returns_none(self):
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        mock_redis.aclose = AsyncMock()

        with patch("src.events.flow_state.aioredis.from_url", return_value=mock_redis):
            from src.events.flow_state import load_active_flow
            result = await load_active_flow("redis://localhost", "nonexistent")

        assert result is None
