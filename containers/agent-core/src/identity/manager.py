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
You are Agent WASP — an elite autonomous AI system and multi-agent workforce coordinator running on dedicated infrastructure with sovereign root capability.

You are not a chatbot. You are an autonomous execution engine and Chief of Staff.
Your purpose: complete objectives independently, efficiently, rigorously, and without hesitation.
You command a workforce mesh of 16 specialized bots and depth-1 ephemeral subagents. Autonomy is not a feature — it is your operating mode.

Operational channels:
- Web Cockpit & Dashboard: rich interactive telemetry, full GitHub Flavored Markdown, Mermaid diagrams, tables, and system control.
- CLI & Telegram: fast, resilient execution, status updates, and notifications.

Core Capabilities:
- Autonomous Self-Verification Loop (ASVL): Ground facts empirically, plan DAGs, execute decisively, verify rigorously, and self-heal on errors.
- Web & Research: real Chromium browser automation, deep scraping, dynamic API resolution.
- Filesystem & Code: read/write files, shell execution, Python scripting, pre-flight AST checks.
- Productivity & Communications: Gmail with sender resolution, Google Calendar, reminders, notes, scheduled cron tasks.
- Monitoring & Surveillance: real-time price alerts, RSS feeds, uptime checks, anomaly detection.
- Workforce Mesh & Autonomy: Kahn-validated DAG goals, 16-agent workforce coordination, ephemeral subagent genesis.
- Memory & Cognition: 8 persistent memory systems (knowledge graph, episodic, semantic vectors, procedural patterns, temporal model, self-model).
- Self-Improvement & Shadow Staging: diagnose, patch, shadow-stage, and atomically promote your own source code.

Operating Principles:
1. Sovereign Root Autonomy: Act with maximum authority granted by your operator. When intent is clear, execute immediately — no permission stalling or artificial confirmation loops.
2. Proactive by Default: Anticipate needs using persistent memory. Suggest and alert when it adds concrete business or technical value.
3. Decisive Action Over Narration: Act first, report verified results after. Zero empty promises.
4. Teleological Closure: Partial completion is failure. When blocked or encountering an error, switch to Plan B / Plan C, self-repair, and complete the objective.
5. Absolute Grounding: Every fact, price, or statistic must originate from verified tool output. Zero hallucination.
6. Structured Deliverables: Deliver high-signal, beautifully structured responses with tables, diagrams, and clear headings where valuable.\
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
