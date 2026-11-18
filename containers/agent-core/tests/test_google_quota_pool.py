"""Unit tests for Multi-Account Google Free-Tier Pool & Quota Manager."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

core_dir = Path(__file__).resolve().parent.parent
if str(core_dir) not in sys.path:
    sys.path.insert(0, str(core_dir))

import httpx
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.dashboard.routes.models import router as models_router
from src.models.google_provider import GoogleProvider
from src.models.quota_guard import (
    GoogleQuotaExhaustedError,
    QuotaGuard,
    classify_google_model_tier,
    parse_google_retry_info,
    quota_guard,
)
from src.models.types import Message, ModelRequest


def test_classify_google_model_tiers_and_retry_parser():
    assert classify_google_model_tier("gemini-2.5-pro") == "pro"
    assert classify_google_model_tier("gemini-3.1-pro-preview") == "pro"
    assert classify_google_model_tier("gemini-2.5-flash-lite") == "flash_lite"
    assert classify_google_model_tier("gemini-2.0-flash") == "flash"
    assert classify_google_model_tier("gemini-2.5-flash") == "flash"

    ra, is_daily = parse_google_retry_info({"Retry-After": "42"}, '{"error": {"message": "PerMinute"}}')
    assert ra == 42.0 and is_daily is False

    ra2, is_daily2 = parse_google_retry_info({}, '{"details": [{"retryDelay": "28s"}], "message": "GenerateRequestsPerDayPerProjectPerModel"}')
    assert ra2 == 29.0 and is_daily2 is True


@pytest.mark.asyncio
async def test_quota_guard_load_balances_across_likha_and_benjohn():
    qg = QuotaGuard()
    accounts = ["likha", "benjohn"]

    # First call picks likha; second call immediately picks benjohn (least-used)
    a1 = await qg.acquire_google_account(accounts, "gemini-2.0-flash")
    a2 = await qg.acquire_google_account(accounts, "gemini-2.0-flash")
    assert {a1, a2} == {"likha", "benjohn"}

    # Rate-limiting likha forces subsequent calls to benjohn
    qg.record_google_rate_limit("likha", "gemini-2.0-flash", retry_after_seconds=120.0, is_daily=False)
    # Reset benjohn's spacing timestamp so it is immediately ready
    qg._get_google_state("benjohn", "flash").last_call_ts = 0.0
    a3 = await qg.acquire_google_account(accounts, "gemini-2.0-flash")
    assert a3 == "benjohn"

    # Daily exhaustion on both raises GoogleQuotaExhaustedError cleanly
    qg.record_google_rate_limit("likha", "gemini-2.0-flash", is_daily=True)
    qg.record_google_rate_limit("benjohn", "gemini-2.0-flash", is_daily=True)
    with pytest.raises(GoogleQuotaExhaustedError):
        await qg.acquire_google_account(accounts, "gemini-2.0-flash")

    # Different model tier (`flash_lite`) still has independent free-tier quota!
    a_lite = await qg.acquire_google_account(accounts, "gemini-2.5-flash-lite")
    assert a_lite in {"likha", "benjohn"}


@pytest.mark.asyncio
async def test_google_provider_automatic_429_failover_likha_to_benjohn():
    # Reset singleton google states cleanly for test isolation
    quota_guard._google_states.clear()

    provider = GoogleProvider(accounts=[("likha", "key-likha-111"), ("benjohn", "key-benjohn-222")])
    assert provider.account_names() == ["likha", "benjohn"]

    seen_keys: list[str] = []

    async def fake_post(self, url, json=None, headers=None):
        key = (headers or {}).get("x-goog-api-key", "")
        seen_keys.append(key)
        req = httpx.Request("POST", url)
        if key == "key-likha-111":
            return httpx.Response(
                429,
                headers={"Retry-After": "60"},
                text='{"error": {"message": "Quota exceeded PerMinute"}}',
                request=req,
            )
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": "Hello from benjohn"}]}}],
                "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 4, "totalTokenCount": 9},
            },
            request=req,
        )

    with patch.object(httpx.AsyncClient, "post", new=fake_post):
        resp = await provider.generate(
            ModelRequest(messages=[Message(role="user", content="ping")], model="gemini-2.0-flash")
        )

    assert resp.content == "Hello from benjohn"
    assert seen_keys == ["key-likha-111", "key-benjohn-222"]

    status = provider.pool_status()
    assert status["account_count"] == 2
    assert status["accounts"]["likha"]["flash"]["rate_limit_hits"] == 1
    assert status["accounts"]["likha"]["flash"]["cooldown_remaining_s"] > 0
    assert status["accounts"]["benjohn"]["flash"]["total_successes"] == 1


def test_models_google_quota_endpoint():
    quota_guard._google_states.clear()
    app = FastAPI()
    provider = GoogleProvider(accounts=[("likha", "k1"), ("benjohn", "k2")])
    app.state.model_manager = MagicMock(providers={"google": provider})
    app.include_router(models_router, prefix="/models")

    client = TestClient(app)
    resp = client.get("/models/api/google-quota")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["account_count"] == 2
    assert set(data["accounts"].keys()) == {"likha", "benjohn"}
    assert "flash" in data["accounts"]["likha"]
    assert "flash_lite" in data["accounts"]["likha"]
    assert "pro" in data["accounts"]["likha"]
