"""Google Gemini provider with Multi-Account Free-Tier Pooling & 429 Failover."""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import time
from typing import Any

import httpx
import structlog

from .base import LLMProvider
from .quota_guard import (
    GoogleQuotaExhaustedError,
    parse_google_retry_info,
    quota_guard,
)
from .types import ModelRequest, ModelResponse, TokenUsage
from ..utils.path_safety import validate_media_path

logger = structlog.get_logger()

GOOGLE_MODELS = [
    # Gemini 2.5 (best free-tier tool calling & speed)
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-2.5-pro",
    # Gemini 3.x (preview)
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite-preview",
    "gemini-3.1-pro-preview",
    # Gemini 1.5 (legacy)
    "gemini-1.5-flash",
    "gemini-1.5-pro",
]
GOOGLE_API_URL = "https://generativelanguage.googleapis.com/v1beta/models"


class GoogleProvider(LLMProvider):
    """Google Gemini provider supporting multi-account free-tier rotation and failover."""

    def __init__(
        self,
        api_key: str = "",
        default_model: str = "gemini-2.5-flash",
        accounts: list[tuple[str, str]] | None = None,
    ):
        self._accounts: dict[str, str] = {}
        seen_keys: set[str] = set()

        for label, key in (accounts or []):
            k = (key or "").strip()
            lbl = (label or "default").strip().lower()
            if k and k not in seen_keys:
                self._accounts[lbl] = k
                seen_keys.add(k)

        if api_key and api_key.strip() and api_key.strip() not in seen_keys:
            lbl = "default" if "default" not in self._accounts else f"account_{len(self._accounts) + 1}"
            self._accounts[lbl] = api_key.strip()
            seen_keys.add(api_key.strip())

        self._api_key = next(iter(self._accounts.values()), "")
        self._default_model = default_model
        self._healthy = False
        self._account_health: dict[str, bool] = {name: False for name in self._accounts}

    def provider_name(self) -> str:
        return "google"

    def account_names(self) -> list[str]:
        return list(self._accounts.keys())

    def pool_status(self) -> dict[str, Any]:
        pool = quota_guard.get_google_pool_status(self.account_names())
        return {
            "healthy": self._healthy,
            "account_health": dict(self._account_health),
            **pool,
        }

    async def _check_one_account(self, client: httpx.AsyncClient, label: str, key: str) -> tuple[str, bool]:
        try:
            resp = await client.get(GOOGLE_API_URL, headers={"x-goog-api-key": key})
            ok = resp.status_code == 200
            if not ok:
                logger.debug("google.account_health_failed", account=label, status_code=resp.status_code)
            return label, ok
        except Exception as exc:
            logger.debug("google.account_health_error", account=label, error=str(exc)[:100])
            return label, False

    async def health_check(self) -> bool:
        if not self._accounts:
            self._healthy = False
            return False
        async with httpx.AsyncClient(timeout=10.0) as client:
            results = await asyncio.gather(
                *(self._check_one_account(client, lbl, key) for lbl, key in self._accounts.items()),
                return_exceptions=True,
            )
        any_healthy = False
        for item in results:
            if isinstance(item, tuple):
                lbl, ok = item
                self._account_health[lbl] = ok
                if ok:
                    any_healthy = True
        self._healthy = any_healthy
        logger.info("google.pool_health_checked", healthy=any_healthy, accounts=self._account_health)
        return any_healthy

    def available_models(self) -> list[str]:
        return GOOGLE_MODELS

    def supports_vision(self, model: str = "") -> bool:
        return True

    def _build_payload(self, request: ModelRequest) -> dict[str, Any]:
        system_text = ""
        contents = []
        for i, m in enumerate(request.messages):
            if m.role == "system":
                system_text += m.content + "\n"
            else:
                role = "user" if m.role == "user" else "model"
                if role == "user" and request.image_path and i == len(request.messages) - 1:
                    parts: list[dict[str, Any]] = [{"text": m.content}]
                    try:
                        safe_path = validate_media_path(request.image_path)
                        with open(safe_path, "rb") as f:
                            img_b64 = base64.b64encode(f.read()).decode()
                        mime, _ = mimetypes.guess_type(safe_path)
                        mime = mime or "image/jpeg"
                        parts.append({"inlineData": {"mimeType": mime, "data": img_b64}})
                    except Exception as exc:
                        logger.warning("google.image_inject_failed", error=str(exc))
                    contents.append({"role": role, "parts": parts})
                else:
                    contents.append({"role": role, "parts": [{"text": m.content}]})

        if system_text and contents:
            first_text = contents[0]["parts"][0]["text"]
            contents[0]["parts"][0]["text"] = f"{system_text.strip()}\n\n{first_text}"

        return {
            "contents": contents,
            "generationConfig": {
                "temperature": request.temperature,
                "maxOutputTokens": request.max_tokens,
            },
        }

    async def generate(self, request: ModelRequest) -> ModelResponse:
        if not self._accounts:
            raise RuntimeError("No Google API keys configured.")

        model = request.model or self._default_model
        payload = self._build_payload(request)
        url = f"{GOOGLE_API_URL}/{model}:generateContent"

        tried: set[str] = set()
        last_error: Exception | None = None
        account_list = self.account_names()

        async with httpx.AsyncClient(timeout=120.0) as client:
            while len(tried) < len(account_list):
                account = await quota_guard.acquire_google_account(
                    account_list,
                    model,
                    exclude=tried,
                )
                key = self._accounts[account]
                start = time.monotonic()

                try:
                    resp = await client.post(
                        url,
                        json=payload,
                        headers={"x-goog-api-key": key},
                    )
                except Exception as exc:
                    tried.add(account)
                    last_error = exc
                    logger.warning("google.account_network_error", account=account, model=model, error=str(exc)[:120])
                    continue

                if resp.status_code == 429:
                    tried.add(account)
                    retry_after, is_daily = parse_google_retry_info(dict(resp.headers), resp.text)
                    quota_guard.record_google_rate_limit(
                        account,
                        model,
                        retry_after_seconds=retry_after,
                        is_daily=is_daily,
                    )
                    last_error = GoogleQuotaExhaustedError(
                        f"Google account '{account}' hit 429 on {model} "
                        f"({'daily' if is_daily else f'cooldown {int(retry_after)}s'})"
                    )
                    logger.warning(
                        "google.account_rate_limited_failover",
                        account=account,
                        model=model,
                        is_daily=is_daily,
                        remaining_accounts=len(account_list) - len(tried),
                    )
                    continue

                if resp.status_code in (401, 403):
                    tried.add(account)
                    self._account_health[account] = False
                    quota_guard.record_google_auth_failure(account, model)
                    last_error = httpx.HTTPStatusError(
                        f"Google account '{account}' unauthorized (HTTP {resp.status_code})",
                        request=resp.request,
                        response=resp,
                    )
                    logger.warning(
                        "google.account_auth_failover",
                        account=account,
                        status_code=resp.status_code,
                        remaining_accounts=len(account_list) - len(tried),
                    )
                    continue

                resp.raise_for_status()
                data = resp.json()
                latency_ms = int((time.monotonic() - start) * 1000)

                quota_guard.record_google_success(account, model)
                self._account_health[account] = True
                self._healthy = True

                candidates = data.get("candidates", [])
                content = ""
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    content = " ".join(p.get("text", "") for p in parts)

                usage_meta = data.get("usageMetadata", {})

                logger.info(
                    "google.generated",
                    account=account,
                    model=model,
                    latency_ms=latency_ms,
                    tokens=usage_meta.get("totalTokenCount", 0),
                )

                return ModelResponse(
                    content=content,
                    model_used=model,
                    provider="google",
                    usage=TokenUsage(
                        prompt_tokens=usage_meta.get("promptTokenCount", 0),
                        completion_tokens=usage_meta.get("candidatesTokenCount", 0),
                        total_tokens=usage_meta.get("totalTokenCount", 0),
                    ),
                    latency_ms=latency_ms,
                )

        if last_error is not None:
            raise last_error
        raise GoogleQuotaExhaustedError(f"All Google accounts exhausted for {model}.")
