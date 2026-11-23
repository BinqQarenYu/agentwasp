"""Token, Rate & Quota Guard for Free Tier AI Providers (including Multi-Account Google Pool).

Enforces:
- RPM (Requests Per Minute) throttling so free-tier rate limits are never exceeded.
- RPD (Requests Per Day) safety caps so daily quotas are respected with safety buffers.
- Per-account AND per-model-tier tracking for Google Gemini Free Tier pools (e.g.
  `likha` + `benjohn`), with least-used load balancing, automatic 429 cooldowns,
  Retry-After parsing, and daily-exhaustion failover.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()


class GoogleQuotaExhaustedError(RuntimeError):
    """Raised when all configured Google Free Tier accounts are exhausted for a model tier."""


# Official Free-Tier limits (with safety buffers to minimize 429s)
DEFAULT_QUOTAS = {
    "google": {
        "max_rpm": 12,
        "max_rpd": 1400,
        "min_request_interval": 4.0,
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
        "max_rpm": 9999,
        "max_rpd": 999999,
        "min_request_interval": 0.0,
    },
    "ollama:cloud": {
        "max_rpm": 30,
        "max_rpd": 3000,
        "min_request_interval": 1.0,
    },
}


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, str(default))))
    except Exception:
        return default


def get_google_tier_limits(tier: str) -> dict[str, float]:
    """Per-account conservative Google AI Studio Free Tier limits by model tier."""
    if tier == "pro":
        return {
            "max_rpm": _env_int("GOOGLE_FREE_PRO_RPM", 4),
            "max_rpd": _env_int("GOOGLE_FREE_PRO_RPD", 90),
            "min_request_interval": _env_float("GOOGLE_FREE_PRO_INTERVAL", 12.0),
        }
    if tier == "flash_lite":
        return {
            "max_rpm": _env_int("GOOGLE_FREE_FLASH_LITE_RPM", 14),
            "max_rpd": _env_int("GOOGLE_FREE_FLASH_LITE_RPD", 950),
            "min_request_interval": _env_float("GOOGLE_FREE_FLASH_LITE_INTERVAL", 3.5),
        }
    # Default: Flash tier (gemini-2.5-flash, gemini-2.0-flash, gemini-3-flash-preview)
    return {
        "max_rpm": _env_int("GOOGLE_FREE_FLASH_RPM", 9),
        "max_rpd": _env_int("GOOGLE_FREE_FLASH_RPD", 240),
        "min_request_interval": _env_float("GOOGLE_FREE_FLASH_INTERVAL", 5.0),
    }


def classify_google_model_tier(model: str) -> str:
    """Classify a Gemini model name into its free-tier quota bucket."""
    m = (model or "").lower()
    if "lite" in m:
        return "flash_lite"
    if "pro" in m:
        return "pro"
    return "flash"


def parse_google_retry_info(headers: dict[str, str] | None, body_text: str = "") -> tuple[float, bool]:
    """Extract `(retry_after_seconds, is_daily_exhausted)` from a Google 429 response."""
    retry_after = 65.0
    if headers:
        raw_ra = headers.get("retry-after") or headers.get("Retry-After")
        if raw_ra:
            try:
                retry_after = max(5.0, float(raw_ra))
            except ValueError:
                pass

    lower = (body_text or "").lower()
    m = re.search(r'"retrydelay"\s*:\s*"(\d+(?:\.\d+)?)s"', lower)
    if m:
        try:
            retry_after = max(5.0, float(m.group(1)) + 1.0)
        except ValueError:
            pass

    is_daily = any(
        marker in lower
        for marker in (
            "perday",
            "per_day",
            "requestsperday",
            "daily limit",
            "quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests",
        )
    ) and ("perminute" not in lower and "per_minute" not in lower)
    return retry_after, is_daily


@dataclass
class ProviderUsageState:
    timestamps: list[float] = field(default_factory=list)
    daily_count: int = 0
    daily_reset_ts: float = field(default_factory=time.time)
    last_call_ts: float = 0.0
    cooldown_until: float = 0.0
    daily_exhausted: bool = False
    rate_limit_hits: int = 0
    total_successes: int = 0


class QuotaGuard:
    """Singleton quota, pacing, and multi-account Google free-tier pool manager."""

    def __init__(self) -> None:
        self._states: dict[str, ProviderUsageState] = {}
        self._google_states: dict[tuple[str, str], ProviderUsageState] = {}
        self._lock = asyncio.Lock()

    def _get_state(self, provider: str) -> ProviderUsageState:
        if provider not in self._states:
            self._states[provider] = ProviderUsageState()
        return self._states[provider]

    def _get_google_state(self, account: str, tier: str) -> ProviderUsageState:
        key = (account, tier)
        if key not in self._google_states:
            self._google_states[key] = ProviderUsageState()
        return self._google_states[key]

    @staticmethod
    def _maybe_reset_daily(state: ProviderUsageState, now: float) -> None:
        if now - state.daily_reset_ts > 86400:
            state.daily_count = 0
            state.daily_exhausted = False
            state.daily_reset_ts = now

    async def acquire_slot(self, provider: str) -> None:
        """Paces execution and waits if rate limit is approaching."""
        async with self._lock:
            now = time.time()
            provider_key = provider.lower()
            cfg = DEFAULT_QUOTAS.get(
                provider_key,
                {
                    "max_rpm": 15,
                    "max_rpd": 1500,
                    "min_request_interval": 3.0,
                },
            )
            state = self._get_state(provider_key)
            self._maybe_reset_daily(state, now)

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

            elapsed_since_last = now - state.last_call_ts
            if elapsed_since_last < cfg["min_request_interval"]:
                wait_time = cfg["min_request_interval"] - elapsed_since_last
                logger.debug("quota_guard.spacing_cooldown", provider=provider, wait_seconds=round(wait_time, 2))
                await asyncio.sleep(wait_time)
                now = time.time()

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
                        sleeping_seconds=round(sleep_needed, 2),
                    )
                    await asyncio.sleep(sleep_needed)
                    now = time.time()
                    state.timestamps = [ts for ts in state.timestamps if now - ts < 60.0]

            state.timestamps.append(now)
            state.daily_count += 1
            state.last_call_ts = now

    async def acquire_google_account(
        self,
        accounts: list[str],
        model: str,
        *,
        exclude: set[str] | None = None,
        max_wait_seconds: float = 65.0,
    ) -> str:
        """Select the best available Google account for `model`, enforcing per-account limits.

        Balances load across accounts (least recent RPM + daily usage first), skips
        accounts in cooldown or at their daily RPD cap, and waits briefly only if all
        remaining accounts are momentarily paced within `max_wait_seconds`.
        """
        if not accounts:
            raise GoogleQuotaExhaustedError("No Google API accounts configured.")

        excluded = exclude or set()
        tier = classify_google_model_tier(model)
        cfg = get_google_tier_limits(tier)

        while True:
            sleep_for: float | None = None
            async with self._lock:
                now = time.time()
                candidates = [a for a in accounts if a not in excluded]
                if not candidates:
                    raise GoogleQuotaExhaustedError(
                        f"All Google accounts ({', '.join(accounts)}) failed or exhausted for tier '{tier}' ({model})."
                    )

                ready: list[tuple[tuple[int, int, float], str, ProviderUsageState]] = []
                wait_options: list[tuple[float, str]] = []

                for acc in candidates:
                    st = self._get_google_state(acc, tier)
                    self._maybe_reset_daily(st, now)
                    st.timestamps = [ts for ts in st.timestamps if now - ts < 60.0]

                    if st.daily_exhausted or st.daily_count >= cfg["max_rpd"]:
                        continue

                    wait_needed = 0.0
                    if st.cooldown_until > now:
                        wait_needed = max(wait_needed, st.cooldown_until - now)

                    elapsed = now - st.last_call_ts
                    if st.last_call_ts > 0 and elapsed < cfg["min_request_interval"]:
                        wait_needed = max(wait_needed, cfg["min_request_interval"] - elapsed)

                    if len(st.timestamps) >= cfg["max_rpm"]:
                        rpm_wait = 60.0 - (now - st.timestamps[0]) + 0.25
                        wait_needed = max(wait_needed, rpm_wait)

                    if wait_needed <= 0.0:
                        score = (len(st.timestamps), st.daily_count, st.last_call_ts)
                        ready.append((score, acc, st))
                    else:
                        wait_options.append((wait_needed, acc))

                if ready:
                    ready.sort(key=lambda item: item[0])
                    _, chosen, st = ready[0]
                    st.timestamps.append(now)
                    st.daily_count += 1
                    st.last_call_ts = now
                    return chosen

                if not wait_options:
                    raise GoogleQuotaExhaustedError(
                        f"Daily Google Free Tier limit reached on all accounts ({', '.join(candidates)}) "
                        f"for tier '{tier}' ({model})."
                    )

                wait_options.sort(key=lambda item: item[0])
                shortest_wait, next_acc = wait_options[0]
                if shortest_wait > max_wait_seconds:
                    raise GoogleQuotaExhaustedError(
                        f"All Google accounts in cooldown for tier '{tier}' "
                        f"(earliest '{next_acc}' ready in {round(shortest_wait, 1)}s)."
                    )
                sleep_for = shortest_wait
                logger.info(
                    "quota_guard.google_pool_pacing",
                    tier=tier,
                    next_account=next_acc,
                    wait_seconds=round(sleep_for, 2),
                )

            if sleep_for is not None and sleep_for > 0:
                await asyncio.sleep(sleep_for)

    def record_google_rate_limit(
        self,
        account: str,
        model: str,
        *,
        retry_after_seconds: float = 65.0,
        is_daily: bool = False,
    ) -> None:
        """Mark a Google account in cooldown (or daily-exhausted) after HTTP 429."""
        tier = classify_google_model_tier(model)
        st = self._get_google_state(account, tier)
        now = time.time()
        st.rate_limit_hits += 1
        if is_daily:
            st.daily_exhausted = True
            logger.warning(
                "quota_guard.google_daily_exhausted",
                account=account,
                tier=tier,
                model=model,
            )
        else:
            st.cooldown_until = max(st.cooldown_until, now + max(5.0, retry_after_seconds))
            logger.warning(
                "quota_guard.google_account_cooldown",
                account=account,
                tier=tier,
                model=model,
                cooldown_seconds=round(st.cooldown_until - now, 1),
            )

    def record_google_auth_failure(self, account: str, model: str, cooldown_seconds: float = 600.0) -> None:
        """Temporarily sideline an account whose key returned 401/403."""
        tier = classify_google_model_tier(model)
        st = self._get_google_state(account, tier)
        st.cooldown_until = max(st.cooldown_until, time.time() + cooldown_seconds)

    def record_google_success(self, account: str, model: str) -> None:
        tier = classify_google_model_tier(model)
        st = self._get_google_state(account, tier)
        st.total_successes += 1

    def get_google_pool_status(self, accounts: list[str] | None = None) -> dict[str, Any]:
        """Return live per-account, per-tier RPM/RPD/cooldown telemetry."""
        now = time.time()
        known_accounts = list(accounts) if accounts is not None else sorted({acc for acc, _ in self._google_states})
        tiers = ("flash", "flash_lite", "pro")
        out_accounts: dict[str, Any] = {}
        for acc in known_accounts:
            acc_tiers: dict[str, Any] = {}
            for tier in tiers:
                cfg = get_google_tier_limits(tier)
                st = self._google_states.get((acc, tier))
                if st is None:
                    acc_tiers[tier] = {
                        "current_rpm": 0,
                        "max_rpm": cfg["max_rpm"],
                        "daily_requests": 0,
                        "daily_limit": cfg["max_rpd"],
                        "quota_remaining_percent": 100.0,
                        "cooldown_remaining_s": 0.0,
                        "daily_exhausted": False,
                        "rate_limit_hits": 0,
                        "total_successes": 0,
                    }
                    continue
                self._maybe_reset_daily(st, now)
                active_rpm = len([ts for ts in st.timestamps if now - ts < 60.0])
                cd = max(0.0, round(st.cooldown_until - now, 1))
                rem_pct = (
                    0.0
                    if st.daily_exhausted
                    else max(0.0, round((1.0 - (st.daily_count / cfg["max_rpd"])) * 100.0, 1))
                )
                acc_tiers[tier] = {
                    "current_rpm": active_rpm,
                    "max_rpm": cfg["max_rpm"],
                    "daily_requests": st.daily_count,
                    "daily_limit": cfg["max_rpd"],
                    "quota_remaining_percent": rem_pct,
                    "cooldown_remaining_s": cd,
                    "daily_exhausted": st.daily_exhausted,
                    "rate_limit_hits": st.rate_limit_hits,
                    "total_successes": st.total_successes,
                }
            out_accounts[acc] = acc_tiers

        return {
            "account_count": len(known_accounts),
            "accounts": out_accounts,
        }

    def get_status(self, provider: str) -> dict:
        """Inspect current usage vs free-tier limits."""
        provider_key = provider.lower()
        if provider_key == "google" and self._google_states:
            pool = self.get_google_pool_status()
            total_rpm = 0
            total_max_rpm = 0
            total_daily = 0
            total_max_daily = 0
            for acc_tiers in pool["accounts"].values():
                fl = acc_tiers["flash"]
                total_rpm += fl["current_rpm"]
                total_max_rpm += fl["max_rpm"]
                total_daily += fl["daily_requests"]
                total_max_daily += fl["daily_limit"]
            return {
                "provider": "google",
                "current_rpm": total_rpm,
                "max_rpm": total_max_rpm or 12,
                "daily_requests": total_daily,
                "daily_limit": total_max_daily or 240,
                "quota_remaining_percent": max(
                    0.0, round((1.0 - (total_daily / max(1, total_max_daily))) * 100.0, 1)
                ),
                "pool": pool,
            }

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
            "quota_remaining_percent": max(0, round((1 - (state.daily_count / cfg.get("max_rpd", 1500))) * 100, 1)),
        }


quota_guard = QuotaGuard()
