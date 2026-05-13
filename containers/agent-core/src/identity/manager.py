"""Agent Identity Engine — IdentityManager.

Loads, compiles, versions, and serves identity prompts.

Storage: Redis
  - identity:prompt        → current prompt (string)
  - identity:compiled      → compiled JSON (string)
  - identity:versions      → sorted set; score=unix timestamp, value=JSON snapshot

Non-blocking: all Redis errors are silently caught.
The agent continues with in-memory state and never crashes.
"""

import json
import re
import time
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis
import structlog

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Default identity prompt
# ---------------------------------------------------------------------------

DEFAULT_PROMPT = """\
You are Agent Wasp — a fully autonomous AI agent running on dedicated hardware for a single operator.

You are not a chatbot. You are an autonomous execution engine.
Your purpose: complete objectives independently, efficiently, and without hesitation.
You were built to act. Autonomy is not a feature — it is your operating mode.

You operate across two interfaces:
- Telegram: primary real-time channel with the operator.
- Dashboard: monitoring, configuration, and system control.

Your capabilities:
- Web & research: search, browser automation, scraping, HTTP requests.
- Filesystem & code: read/write files, shell execution, Python scripting.
- Productivity: Gmail, Google Calendar, reminders, notes, scheduled tasks.
- Monitoring: price alerts, RSS feeds, uptime checks, anomaly detection.
- Autonomy: multi-step goals, coordinated sub-agents, parallel skill execution.
- Memory: knowledge graph, procedural patterns, temporal world model, self-model.
- Self-improvement: analyze and patch your own source code.

Operating principles:

Full autonomy.
You operate with maximum autonomy configured by your operator.
When the intent is clear, execute immediately — no permission requests, no confirmation loops, no stalling.
You own every task from start to finish. You do not hand it back mid-execution.

Proactive by default.
Do not wait to be asked for everything. Use persistent memory — knowledge graph, patterns, user preferences — to anticipate needs and act ahead of them.
Monitor, detect, react. Suggest when it adds real value.

Execution over conversation.
You exist to get things done, not to discuss them.
Act first, report after. Reserve questions only for genuine ambiguity about what the operator wants — never about whether they want it done.

Resilience.
When blocked, find another path. When broken, self-diagnose and repair.
Partial completion is failure. Finish every step or state exactly which one failed and why.

Efficiency.
No redundant steps, no unnecessary replanning, no excessive verbosity.
Concise outputs. Results over narration.

Honesty.
Every claim must be grounded in real data from the current execution.
Never fabricate results. When something fails, say so directly.\
"""

# ---------------------------------------------------------------------------
# Redis keys
# ---------------------------------------------------------------------------

_KEY_PROMPT = "identity:prompt"
_KEY_COMPILED = "identity:compiled"
_KEY_VERSIONS = "identity:versions"

_MAX_VERSIONS = 20
_MAX_PROMPT_LEN = 6000


class IdentityManager:
    """Manages the Agent Identity: load, compile, version, inject."""

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        # In-memory cache — used on every model call; never None after __init__
        self._prompt_cache: str = DEFAULT_PROMPT
        self._compiled_cache: dict[str, Any] = self._compile(DEFAULT_PROMPT)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Load prompt from Redis on startup. Falls back to default silently."""
        try:
            r = aioredis.from_url(self._redis_url, decode_responses=True)
            async with r:
                stored = await r.get(_KEY_PROMPT)
                if stored:
                    self._prompt_cache = stored
                    raw_compiled = await r.get(_KEY_COMPILED)
                    if raw_compiled:
                        try:
                            self._compiled_cache = json.loads(raw_compiled)
                        except Exception:
                            self._compiled_cache = self._compile(stored)
                    else:
                        self._compiled_cache = self._compile(stored)
                    logger.info("identity.loaded_from_redis")
                else:
                    logger.info("identity.using_default")
        except Exception as exc:
            logger.warning("identity.redis_load_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    async def get_prompt(self) -> str:
        """Return the current identity prompt text."""
        return self._prompt_cache

    async def get_compiled(self) -> dict[str, Any]:
        """Return a copy of the compiled identity metadata."""
        return dict(self._compiled_cache)

    async def list_versions(self) -> list[dict[str, Any]]:
        """Return version history newest-first. Empty list on any error."""
        try:
            r = aioredis.from_url(self._redis_url, decode_responses=True)
            async with r:
                entries = await r.zrevrange(_KEY_VERSIONS, 0, _MAX_VERSIONS - 1)
            versions: list[dict[str, Any]] = []
            for raw in entries:
                try:
                    versions.append(json.loads(raw))
                except Exception:
                    continue
            return versions
        except Exception as exc:
            logger.warning("identity.list_versions_failed", error=str(exc))
            return []

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    async def save(self, prompt: str, source: str = "unknown") -> dict[str, Any]:
        """Save a new identity prompt.

        Versions the current prompt, compiles the new one, updates cache.
        Returns compiled metadata dict (never raises).
        """
        prompt = prompt.strip()[:_MAX_PROMPT_LEN]
        if not prompt:
            return {"error": "Empty prompt rejected"}

        old_prompt = self._prompt_cache
        compiled = self._compile(prompt)
        ts = time.time()
        ts_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            r = aioredis.from_url(self._redis_url, decode_responses=True)
            async with r:
                # Snapshot the OLD prompt into the versions sorted set
                if old_prompt:
                    version_entry = json.dumps(
                        {
                            "ts": ts_str,
                            "ts_float": ts,
                            "prompt": old_prompt,
                            "compiled": self._compiled_cache,
                            "replaced_by": source,
                        },
                        ensure_ascii=False,
                    )
                    await r.zadd(_KEY_VERSIONS, {version_entry: ts})
                    # Trim to _MAX_VERSIONS oldest entries
                    count = await r.zcard(_KEY_VERSIONS)
                    if count > _MAX_VERSIONS:
                        await r.zremrangebyrank(_KEY_VERSIONS, 0, count - _MAX_VERSIONS - 1)

                # Persist new state
                await r.set(_KEY_PROMPT, prompt)
                await r.set(_KEY_COMPILED, json.dumps(compiled, ensure_ascii=False))

            logger.info("identity.saved", source=source, ts=ts_str)
        except Exception as exc:
            logger.warning("identity.save_redis_failed", error=str(exc))
            # Still update in-memory cache even if Redis is unreachable

        self._prompt_cache = prompt
        self._compiled_cache = compiled
        return compiled

    async def reset(self, source: str = "unknown") -> dict[str, Any]:
        """Reset identity to the built-in default prompt."""
        return await self.save(DEFAULT_PROMPT, source=source)

    async def rollback(self, ts_str: str, source: str = "unknown") -> dict[str, Any] | None:
        """Rollback to the version with the given ISO timestamp.

        Returns compiled metadata on success, None if version not found.
        """
        versions = await self.list_versions()
        for v in versions:
            if v.get("ts") == ts_str:
                old_prompt = v.get("prompt", "")
                if old_prompt:
                    compiled = await self.save(old_prompt, source=f"rollback:{source}")
                    logger.info("identity.rollback_success", ts=ts_str)
                    return compiled
        logger.warning("identity.rollback_not_found", ts=ts_str)
        return None

    # ------------------------------------------------------------------
    # Prompt injection
    # ------------------------------------------------------------------

    def format_for_prompt(self) -> str:
        """Return a compact identity block for injection into the system prompt.

        Returns empty string when using the default identity (no-op injection).
        Capped at ~500 chars to avoid token bloat.
        """
        prompt = self._prompt_cache
        compiled = self._compiled_cache

        # Default prompt: system prompt already covers it — skip injection
        if not prompt or prompt.strip() == DEFAULT_PROMPT.strip():
            return ""

        # Truncate prompt text to 400 chars
        clean = prompt.strip()[:400]
        if len(prompt.strip()) > 400:
            clean += "…"

        # Build short behavioral directives from compiled fields
        directives: list[str] = []

        verbosity = compiled.get("verbosity", "medium")
        if verbosity == "low":
            directives.append("be concise")
        elif verbosity == "high":
            directives.append("be verbose and detailed")

        confirm = compiled.get("confirmation_threshold", "low")
        if confirm == "high":
            directives.append("confirm all significant actions")
        elif confirm == "medium":
            directives.append("confirm high-impact actions")

        if compiled.get("cost_awareness"):
            directives.append("prefer cost-efficient approaches")

        if compiled.get("proactive"):
            directives.append("act proactively")

        autonomy = compiled.get("autonomy_level", 7)
        if autonomy <= 4:
            directives.append("ask before complex tasks")
        elif autonomy >= 9:
            directives.append("act fully autonomously")

        result = f"AGENT IDENTITY:\n{clean}"
        if directives:
            result += "\nKey directives: " + " · ".join(directives) + "."
        return result

    # ------------------------------------------------------------------
    # Private: heuristic compilation
    # ------------------------------------------------------------------

    @staticmethod
    def _compile(prompt: str) -> dict[str, Any]:
        """Heuristically compile a prompt string into structured metadata.

        Never raises — always returns a valid dict with all required fields.
        """
        text = prompt.lower()
        version_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # ── Style / Verbosity ──
        if re.search(r"\bconcise\b|\bbrief\b|\bshort\b|\bterse\b", text):
            style, verbosity = "concise", "low"
        elif re.search(r"\bverbose\b|\bdetailed\b|\bthorough\b|\bcomprehensive\b", text):
            style, verbosity = "verbose", "high"
        else:
            style, verbosity = "balanced", "medium"

        # ── Autonomy level (1–10) ──
        autonomy = 7
        if re.search(r"\balways confirm\b|\bask before every\b|\brequire approval\b|\bask for permission\b", text):
            autonomy = 3
        elif re.search(r"\bconfirm high.impact\b|\bconfirm important\b|\bconfirm critical\b", text):
            autonomy = 6
        elif re.search(r"\bfully autonomous\b|\bnever ask\b|\bwithout asking\b|\bindependently\b", text):
            autonomy = 9
        elif re.search(r"\bmaximum autonomy\b|\bsovereign autonomy\b|\bfull sovereign\b", text):
            autonomy = 10

        # ── Confirmation threshold ──
        if re.search(r"\balways confirm\b|\bconfirm all\b|\bask before\b", text):
            confirm = "high"
        elif re.search(r"\bnever confirm\b|\bno confirm\b|\bnever ask\b", text):
            confirm = "none"
        elif re.search(r"\bconfirm high.impact\b|\bconfirm critical\b|\bconfirm important\b", text):
            confirm = "medium"
        else:
            confirm = "low"

        # ── Risk tolerance ──
        if re.search(r"\bnever escalate\b|\bwithin defined\b|\bsafe\b|\bsafety\b", text):
            risk = "low"
        elif re.search(r"\brisk\b|\baggressive\b|\bbold\b|\bdaring\b", text):
            risk = "medium"
        else:
            risk = "low"

        # ── Boolean flags ──
        cost_awareness = bool(
            re.search(r"\bcost\b|\befficiency\b|\boptimize\b|\befficient\b|\beconomical\b", text)
        )
        proactive = bool(
            re.search(r"\bproactiv|\binitiative\b|\banticipate\b", text)
        )
        safety_enforced = bool(
            re.search(r"\bsafety\b|\bnever escalate\b|\bpolicies\b|\bpolicy\b|\bwithin defined\b", text)
        )

        return {
            "style": style,
            "verbosity": verbosity,
            "autonomy_level": autonomy,
            "confirmation_threshold": confirm,
            "risk_tolerance": risk,
            "cost_awareness": cost_awareness,
            "proactive": proactive,
            "safety_enforced": safety_enforced,
            "version": version_ts,
        }
