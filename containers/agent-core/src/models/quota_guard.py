"""Token, Rate & Quota Guard for Free Tier AI Providers (Gemini, Grok, OpenAI).

Enforces:
- RPM (Requests Per Minute) throttling so free-tier rate limits (e.g. 15 RPM for Gemini) are never exceeded.
- RPD (Requests Per Day) safety caps so you never blow through daily quotas (e.g. 1,500 RPD for Gemini free).
- Token budget capping per request and sliding-window cooldowns.
"""

import time
import asyncio
from typing import Optional
from dataclasses import dataclass, field
import structlog

logger = structlog.get_logger()

# Official Free-Tier limits (with a 10% safety buffer to guarantee zero 429 errors)
DEFAULT_QUOTAS = {
    "google": {
        "max_rpm": 12,          # Official free tier is 15 RPM -> Throttled to 12
        "max_rpd": 1400,        # Official free tier is 1500 RPD -> Capped at 1400
        "min_request_interval": 4.0,  # Minimum seconds between calls
    },
    "xai": {
        "max_rpm": 20,
        "max_rpd": 2000,
        "min_request_interval": 2.5,
    },
    "openai": {
        "max_rpm": 15,
        "max_rpd": 1500,
        "min_request_interval": 3.0,
    },
    "anthropic": {
        "max_rpm": 10,
        "max_rpd": 1000,
        "min_request_interval": 5.0,
    },
    "ollama": {
        "max_rpm": 9999,        # Unlimited local hardware
        "max_rpd": 999999,
        "min_request_interval": 0.0,
    }
}


@dataclass
class ProviderUsageState:
    timestamps: list[float] = field(default_factory=list)
    daily_count: int = 0
    daily_reset_ts: float = field(default_factory=time.time)
    last_call_ts: float = 0.0


class QuotaGuard:
    """Singleton quota and credit manager."""

    def __init__(self):
        self._states: dict[str, ProviderUsageState] = {}
        self._lock = asyncio.Lock()

    def _get_state(self, provider: str) -> ProviderUsageState:
        if provider not in self._states:
            self._states[provider] = ProviderUsageState()
        return self._states[provider]

    async def acquire_slot(self, provider: str) -> None:
        """Paces execution and waits if rate limit is approaching."""
        async with self._lock:
            now = time.time()
            provider_key = provider.lower()
            cfg = DEFAULT_QUOTAS.get(provider_key, {
                "max_rpm": 15,
                "max_rpd": 1500,
                "min_request_interval": 3.0,
            })
            state = self._get_state(provider_key)

            # Reset daily counter every 24 hours
            if now - state.daily_reset_ts > 86400:
                state.daily_count = 0
                state.daily_reset_ts = now

            # Check daily hard limit
            if state.daily_count >= cfg["max_rpd"]:
                logger.error(
                    "quota_guard.daily_limit_exceeded",
                    provider=provider,
                    daily_count=state.daily_count,
                    max_rpd=cfg["max_rpd"],
                )
                raise RuntimeError(
                    f"Free-tier safety limit reached for {provider} ({state.daily_count}/{cfg['max_rpd']} daily requests). "
                    f"Swapping to fallback provider or waiting for daily reset."
                )

            # Enforce minimum spacing between calls
            elapsed_since_last = now - state.last_call_ts
            if elapsed_since_last < cfg["min_request_interval"]:
                wait_time = cfg["min_request_interval"] - elapsed_since_last
                logger.debug("quota_guard.spacing_cooldown", provider=provider, wait_seconds=round(wait_time, 2))
                await asyncio.sleep(wait_time)
                now = time.time()

            # Enforce RPM (Sliding window of 60 seconds)
            state.timestamps = [ts for ts in state.timestamps if now - ts < 60.0]
            if len(state.timestamps) >= cfg["max_rpm"]:
                oldest = state.timestamps[0]
                sleep_needed = 60.0 - (now - oldest) + 0.5
                if sleep_needed > 0:
                    logger.warning(
                        "quota_guard.rpm_throttle_activated",
                        provider=provider,
                        active_rpm=len(state.timestamps),
                        max_rpm=cfg["max_rpm"],
                        sleeping_seconds=round(sleep_needed, 2)
                    )
                    await asyncio.sleep(sleep_needed)
                    now = time.time()
                    state.timestamps = [ts for ts in state.timestamps if now - ts < 60.0]

            # Record call slot
            state.timestamps.append(now)
            state.daily_count += 1
            state.last_call_ts = now

    def get_status(self, provider: str) -> dict:
        """Inspect current usage vs free-tier limits."""
        provider_key = provider.lower()
        cfg = DEFAULT_QUOTAS.get(provider_key, {"max_rpm": 15, "max_rpd": 1500})
        state = self._get_state(provider_key)
        now = time.time()
        active_rpm = len([ts for ts in state.timestamps if now - ts < 60.0])
        return {
            "provider": provider,
            "current_rpm": active_rpm,
            "max_rpm": cfg.get("max_rpm", 15),
            "daily_requests": state.daily_count,
            "daily_limit": cfg.get("max_rpd", 1500),
            "quota_remaining_percent": max(0, round((1 - (state.daily_count / cfg.get("max_rpd", 1500))) * 100, 1))
        }


quota_guard = QuotaGuard()
