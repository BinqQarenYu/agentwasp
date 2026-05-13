"""Economic efficiency tracker for WASP agent.

Tracks:
- Cost per model call (using known pricing tables)
- Cost per project
- Cost per day
- Performance-per-cost ratio

All costs are estimates based on public pricing.
Actual costs depend on your API plan.

Never blocks the agent — all updates are in-memory.
Redis persistence is optional and fire-and-forget.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

import structlog

logger = structlog.get_logger()

# Token pricing (USD per 1M tokens) — input / output
# Sources: official provider pricing pages, verified April 2026
PRICING: dict[str, dict] = {
    # ── Anthropic ──────────────────────────────────────────────────────────
    "claude-opus-4-6":              {"input": 15.0,   "output": 75.0},
    "claude-sonnet-4-6":            {"input": 3.0,    "output": 15.0},
    "claude-haiku-4-5":             {"input": 0.8,    "output": 4.0},
    "claude-haiku-4-5-20251001":    {"input": 0.8,    "output": 4.0},
    "claude-3-5-sonnet-20241022":   {"input": 3.0,    "output": 15.0},
    "claude-3-5-haiku-20241022":    {"input": 0.8,    "output": 4.0},
    "claude-3-opus-20240229":       {"input": 15.0,   "output": 75.0},
    "claude-3-sonnet-20240229":     {"input": 3.0,    "output": 15.0},
    "claude-3-haiku-20240307":      {"input": 0.25,   "output": 1.25},
    # ── OpenAI ────────────────────────────────────────────────────────────
    "gpt-4.1":                      {"input": 2.0,    "output": 8.0},
    "gpt-4.1-mini":                 {"input": 0.4,    "output": 1.6},
    "gpt-4.1-nano":                 {"input": 0.1,    "output": 0.4},
    "gpt-4o":                       {"input": 2.5,    "output": 10.0},
    "gpt-4o-mini":                  {"input": 0.15,   "output": 0.6},
    "gpt-4-turbo":                  {"input": 10.0,   "output": 30.0},
    "gpt-4":                        {"input": 30.0,   "output": 60.0},
    "gpt-3.5-turbo":                {"input": 0.5,    "output": 1.5},
    "o1":                           {"input": 15.0,   "output": 60.0},
    "o1-mini":                      {"input": 3.0,    "output": 12.0},
    "o1-preview":                   {"input": 15.0,   "output": 60.0},
    "o3":                           {"input": 10.0,   "output": 40.0},
    "o3-mini":                      {"input": 1.1,    "output": 4.4},
    "o4-mini":                      {"input": 1.1,    "output": 4.4},
    # ── Google ────────────────────────────────────────────────────────────
    "gemini-2.5-pro":               {"input": 1.25,   "output": 10.0},
    "gemini-2.5-flash":             {"input": 0.15,   "output": 0.6},
    "gemini-2.0-flash":             {"input": 0.1,    "output": 0.4},
    "gemini-2.0-flash-lite":        {"input": 0.075,  "output": 0.3},
    "gemini-1.5-pro":               {"input": 1.25,   "output": 5.0},
    "gemini-1.5-flash":             {"input": 0.075,  "output": 0.3},
    "gemini-1.5-flash-8b":          {"input": 0.0375, "output": 0.15},
    # ── xAI / Grok ────────────────────────────────────────────────────────
    "grok-3":                       {"input": 3.0,    "output": 15.0},
    "grok-3-mini":                  {"input": 0.3,    "output": 0.5},
    "grok-2":                       {"input": 2.0,    "output": 10.0},
    "grok-2-mini":                  {"input": 0.2,    "output": 0.4},
    "grok-beta":                    {"input": 5.0,    "output": 15.0},
    # ── Mistral ───────────────────────────────────────────────────────────
    "mistral-large-latest":         {"input": 3.0,    "output": 9.0},
    "mistral-medium-latest":        {"input": 2.7,    "output": 8.1},
    "mistral-small-latest":         {"input": 0.2,    "output": 0.6},
    "codestral-latest":             {"input": 0.3,    "output": 0.9},
    "mistral-7b-instruct":          {"input": 0.25,   "output": 0.25},
    # ── DeepSeek ──────────────────────────────────────────────────────────
    "deepseek-chat":                {"input": 0.27,   "output": 1.1},
    "deepseek-reasoner":            {"input": 0.55,   "output": 2.19},
    "deepseek-coder":               {"input": 0.27,   "output": 1.1},
    # ── Moonshot / Kimi ───────────────────────────────────────────────────
    "moonshot-v1-8k":               {"input": 1.67,   "output": 1.67},
    "moonshot-v1-32k":              {"input": 3.33,   "output": 3.33},
    "moonshot-v1-128k":             {"input": 16.67,  "output": 16.67},
    # ── Perplexity ────────────────────────────────────────────────────────
    "sonar-pro":                    {"input": 3.0,    "output": 15.0},
    "llama-3.1-sonar-large-128k-online": {"input": 1.0, "output": 1.0},
    "llama-3.1-sonar-small-128k-online": {"input": 0.2, "output": 0.2},
    # ── Local (zero cost) ─────────────────────────────────────────────────
    "_local_":                      {"input": 0.0,    "output": 0.0},
}

# Prefix → pricing for versioned model IDs (e.g. gpt-4o-2024-11-20 → gpt-4o)
# Keys ordered longest-first so most-specific prefix wins.
_PREFIX_MAP: list[tuple[str, str]] = [
    # OpenAI — must check mini/nano before base
    ("gpt-4.1-mini",    "gpt-4.1-mini"),
    ("gpt-4.1-nano",    "gpt-4.1-nano"),
    ("gpt-4.1",         "gpt-4.1"),
    ("gpt-4o-mini",     "gpt-4o-mini"),
    ("gpt-4o",          "gpt-4o"),
    ("gpt-4-turbo",     "gpt-4-turbo"),
    ("gpt-4",           "gpt-4"),
    ("gpt-3.5-turbo",   "gpt-3.5-turbo"),
    ("o4-mini",         "o4-mini"),
    ("o3-mini",         "o3-mini"),
    ("o3",              "o3"),
    ("o1-mini",         "o1-mini"),
    ("o1-preview",      "o1-preview"),
    ("o1",              "o1"),
    # Anthropic
    ("claude-opus-4",   "claude-opus-4-6"),
    ("claude-sonnet-4", "claude-sonnet-4-6"),
    ("claude-haiku-4",  "claude-haiku-4-5"),
    ("claude-3-5-sonnet", "claude-3-5-sonnet-20241022"),
    ("claude-3-5-haiku",  "claude-3-5-haiku-20241022"),
    ("claude-3-opus",   "claude-3-opus-20240229"),
    ("claude-3-haiku",  "claude-3-haiku-20240307"),
    # Google
    ("gemini-2.5-pro",   "gemini-2.5-pro"),
    ("gemini-2.5-flash", "gemini-2.5-flash"),
    ("gemini-2.0-flash-lite", "gemini-2.0-flash-lite"),
    ("gemini-2.0-flash", "gemini-2.0-flash"),
    ("gemini-1.5-pro",   "gemini-1.5-pro"),
    ("gemini-1.5-flash-8b", "gemini-1.5-flash-8b"),
    ("gemini-1.5-flash", "gemini-1.5-flash"),
    # xAI
    ("grok-3-mini", "grok-3-mini"),
    ("grok-3",      "grok-3"),
    ("grok-2-mini", "grok-2-mini"),
    ("grok-2",      "grok-2"),
    # Mistral
    ("mistral-large",  "mistral-large-latest"),
    ("mistral-medium", "mistral-medium-latest"),
    ("mistral-small",  "mistral-small-latest"),
    ("codestral",      "codestral-latest"),
    # DeepSeek
    ("deepseek-reasoner", "deepseek-reasoner"),
    ("deepseek-coder",    "deepseek-coder"),
    ("deepseek-chat",     "deepseek-chat"),
    ("deepseek-r1",       "deepseek-reasoner"),
    ("deepseek-v3",       "deepseek-chat"),
    # Moonshot
    ("moonshot-v1-128k", "moonshot-v1-128k"),
    ("moonshot-v1-32k",  "moonshot-v1-32k"),
    ("moonshot-v1-8k",   "moonshot-v1-8k"),
]

# Model name prefixes that indicate a local/free model (Ollama, LM Studio, etc.)
# Deliberately narrow — only patterns that are NEVER used for paid APIs
_LOCAL_PREFIXES = (
    "llama", "qwen", "phi", "gemma", "yi", "codellama",
    "falcon", "vicuna", "orca", "neural", "openchat", "hermes",
    "stablelm", "tinyllama", "starcoder", "smollm",
)

# Fallback for truly unknown models
_DEFAULT_PRICING = {"input": 1.0, "output": 3.0}


def _get_pricing(model: str) -> dict:
    """Return pricing dict for a model, with fallback to prefix matching."""
    if not model:
        return _DEFAULT_PRICING
    if model in PRICING:
        return PRICING[model]
    # Versioned-ID prefix matching (e.g. gpt-4o-2024-11-20 → gpt-4o)
    for prefix, canonical in _PREFIX_MAP:
        if model.startswith(prefix):
            return PRICING.get(canonical, _DEFAULT_PRICING)
    return _DEFAULT_PRICING


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int, provider: str = "") -> float:
    """Estimate cost in USD for a model call.

    Returns 0.0 for local/Ollama models (no API cost).
    """
    # Ollama provider is always free (local inference)
    if provider == "ollama":
        return 0.0

    # Local model name patterns — free (only truly local-only patterns)
    if model and not model.startswith(("gpt", "claude", "gemini", "grok", "o1", "o3", "o4",
                                        "mistral-large", "mistral-medium", "mistral-small",
                                        "codestral", "deepseek", "moonshot", "sonar")) \
            and model.startswith(_LOCAL_PREFIXES) \
            and "/" not in model:
        return 0.0

    pricing = _get_pricing(model)
    input_cost = (prompt_tokens / 1_000_000) * pricing["input"]
    output_cost = (completion_tokens / 1_000_000) * pricing["output"]
    return round(input_cost + output_cost, 8)


@dataclass
class ModelCostEntry:
    model: str
    provider: str
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class DailyCostEntry:
    date: str
    calls: int = 0
    tokens: int = 0
    cost_usd: float = 0.0


class EconomicsTracker:
    """Tracks cost efficiency across models, projects, and time windows.

    Persists to Redis so data survives container restarts.
    Redis keys:
      economics:lifetime          HASH  — total_calls, total_tokens, cost_usd
      economics:model:{name}      HASH  — calls, prompt_tokens, completion_tokens, cost_usd, provider
      economics:day:{YYYY-MM-DD}  HASH  — calls, tokens, cost_usd  (TTL 31d)
    """

    def __init__(self):
        self._redis_url: str | None = None
        # model_name → ModelCostEntry
        self._by_model: dict[str, ModelCostEntry] = {}
        # project_id → total cost
        self._by_project: dict[str, float] = {}
        # YYYY-MM-DD → DailyCostEntry
        self._by_day: dict[str, DailyCostEntry] = {}
        # Total lifetime
        self._total_calls = 0
        self._total_cost = 0.0
        self._total_tokens = 0

    # ── Redis persistence ──────────────────────────────────────────────────

    async def load_from_redis(self) -> None:
        """Restore accumulated state from Redis on startup. Safe to call even if Redis is down."""
        if not self._redis_url:
            return
        try:
            import redis.asyncio as aioredis
            r = aioredis.from_url(self._redis_url, decode_responses=True)

            # Lifetime totals
            lt = await r.hgetall("economics:lifetime")
            if lt:
                self._total_calls  = int(float(lt.get("total_calls", 0)))
                self._total_tokens = int(float(lt.get("total_tokens", 0)))
                self._total_cost   = round(float(lt.get("cost_usd", 0)), 8)

            # Per-model entries
            model_keys = await r.keys("economics:model:*")
            for key in model_keys:
                model_name = key[len("economics:model:"):]
                d = await r.hgetall(key)
                if d:
                    self._by_model[model_name] = ModelCostEntry(
                        model=model_name,
                        provider=d.get("provider", ""),
                        calls=int(float(d.get("calls", 0))),
                        prompt_tokens=int(float(d.get("prompt_tokens", 0))),
                        completion_tokens=int(float(d.get("completion_tokens", 0))),
                        cost_usd=round(float(d.get("cost_usd", 0)), 8),
                    )

            # Per-day entries
            day_keys = await r.keys("economics:day:*")
            for key in day_keys:
                date_str = key[len("economics:day:"):]
                d = await r.hgetall(key)
                if d:
                    self._by_day[date_str] = DailyCostEntry(
                        date=date_str,
                        calls=int(float(d.get("calls", 0))),
                        tokens=int(float(d.get("tokens", 0))),
                        cost_usd=round(float(d.get("cost_usd", 0)), 8),
                    )

            await r.aclose()
            logger.info(
                "economics.restored_from_redis",
                models=len(self._by_model),
                lifetime_cost=self._total_cost,
            )
        except Exception as exc:
            logger.warning("economics.load_from_redis.failed", error=str(exc)[:120])

    def _persist_fire_and_forget(
        self,
        model: str,
        provider: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost: float,
        today: str,
    ) -> None:
        """Non-blocking Redis persistence after each record call."""
        if not self._redis_url:
            return
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._persist_record(
                    model, provider, prompt_tokens, completion_tokens, cost, today
                ))
        except RuntimeError:
            pass

    async def _persist_record(
        self,
        model: str,
        provider: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost: float,
        today: str,
    ) -> None:
        try:
            import redis.asyncio as aioredis
            r = aioredis.from_url(self._redis_url, decode_responses=True)
            pipe = r.pipeline(transaction=False)

            # Per-model
            mkey = f"economics:model:{model}"
            pipe.hset(mkey, "provider", provider)
            pipe.hincrbyfloat(mkey, "calls", 1)
            pipe.hincrbyfloat(mkey, "prompt_tokens", prompt_tokens)
            pipe.hincrbyfloat(mkey, "completion_tokens", completion_tokens)
            pipe.hincrbyfloat(mkey, "cost_usd", cost)

            # Per-day
            dkey = f"economics:day:{today}"
            pipe.hincrbyfloat(dkey, "calls", 1)
            pipe.hincrbyfloat(dkey, "tokens", prompt_tokens + completion_tokens)
            pipe.hincrbyfloat(dkey, "cost_usd", cost)
            pipe.expire(dkey, 86400 * 31)

            # Lifetime
            pipe.hincrbyfloat("economics:lifetime", "total_calls", 1)
            pipe.hincrbyfloat("economics:lifetime", "total_tokens", prompt_tokens + completion_tokens)
            pipe.hincrbyfloat("economics:lifetime", "cost_usd", cost)

            await pipe.execute()
            await r.aclose()
        except Exception:
            pass  # persistence is best-effort, never crash for this

    # ── Recording ──────────────────────────────────────────────────────────

    def record(
        self,
        model: str,
        provider: str,
        prompt_tokens: int,
        completion_tokens: int,
        project_id: str = "default",
    ) -> float:
        """Record a model call. Returns estimated cost in USD."""
        cost = estimate_cost(model, prompt_tokens, completion_tokens, provider=provider)
        total_tokens = prompt_tokens + completion_tokens

        # Per-model tracking
        if model not in self._by_model:
            self._by_model[model] = ModelCostEntry(model=model, provider=provider)
        entry = self._by_model[model]
        entry.calls += 1
        entry.prompt_tokens += prompt_tokens
        entry.completion_tokens += completion_tokens
        entry.cost_usd = round(entry.cost_usd + cost, 8)

        # Per-project tracking
        self._by_project[project_id] = round(
            self._by_project.get(project_id, 0.0) + cost, 8
        )

        # Daily tracking
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today not in self._by_day:
            self._by_day[today] = DailyCostEntry(date=today)
            if len(self._by_day) > 30:
                oldest = sorted(self._by_day.keys())[0]
                del self._by_day[oldest]
        day = self._by_day[today]
        day.calls += 1
        day.tokens += total_tokens
        day.cost_usd = round(day.cost_usd + cost, 8)

        # Lifetime totals
        self._total_calls += 1
        self._total_cost = round(self._total_cost + cost, 8)
        self._total_tokens += total_tokens

        # Persist to Redis (non-blocking)
        self._persist_fire_and_forget(model, provider, prompt_tokens, completion_tokens, cost, today)

        return cost

    def get_summary(self) -> dict:
        """Return economics summary. Always fast (in-memory)."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_entry = self._by_day.get(today, DailyCostEntry(date=today))

        # Performance-per-cost ratio (tasks per dollar, higher is better)
        # Cap at 9999 — displays as "∞" in UI when cost is effectively zero
        if self._total_cost > 0:
            perf_ratio = min(round(self._total_calls / self._total_cost, 1), 9999.0)
        else:
            perf_ratio = None  # means "∞" (no cost yet)

        return {
            "lifetime": {
                "calls": self._total_calls,
                "tokens": self._total_tokens,
                "cost_usd": self._total_cost,
            },
            "today": {
                "calls": today_entry.calls,
                "tokens": today_entry.tokens,
                "cost_usd": today_entry.cost_usd,
            },
            "by_model": [
                {
                    "model": e.model,
                    "provider": e.provider,
                    "calls": e.calls,
                    "input_tokens": e.prompt_tokens,
                    "output_tokens": e.completion_tokens,
                    "tokens": e.prompt_tokens + e.completion_tokens,
                    "cost_usd": e.cost_usd,
                    "avg_cost_per_call": round(e.cost_usd / max(e.calls, 1), 8),
                }
                for e in sorted(
                    self._by_model.values(), key=lambda x: x.cost_usd, reverse=True
                )[:10]
            ],
            "by_project": sorted(
                [{"project": k, "cost_usd": v} for k, v in self._by_project.items()],
                key=lambda x: x["cost_usd"],
                reverse=True,
            )[:10],
            "recent_days": [
                {"date": e.date, "calls": e.calls, "cost_usd": e.cost_usd}
                for e in sorted(self._by_day.values(), key=lambda x: x.date, reverse=True)[:7]
            ],
            "performance_per_cost": perf_ratio,
        }

    def get_model_efficiency(self) -> list[dict]:
        """Rank models by cost efficiency (tasks per dollar)."""
        result = []
        for model, entry in self._by_model.items():
            if entry.cost_usd > 0:
                efficiency = round(entry.calls / entry.cost_usd, 2)
            else:
                efficiency = float("inf")  # Local/free models are infinitely efficient
            avg_cost = round(entry.cost_usd / max(entry.calls, 1), 8)
            result.append({
                "model": model,
                "provider": entry.provider,
                "calls": entry.calls,
                "cost_usd": entry.cost_usd,
                "avg_cost_per_call": avg_cost,
                "calls_per_dollar": efficiency if efficiency != float("inf") else None,
            })
        return sorted(result, key=lambda x: x["cost_usd"], reverse=True)


# Module-level singleton
economics = EconomicsTracker()
