"""Capability Evolution Engine — autonomously extends WASP's skill set.

Detects capability gaps when the agent repeatedly fails a task or when
SelfReflectionEngine identifies missing abilities. Then:
  1. Scores the gap (must exceed threshold to avoid noise)
  2. Generates candidate skill code via LLM
  3. Validates in sandbox (AST parse + security blocklist + structural checks)
  4. Registers the new skill in SkillRegistry at SAFE capability level
  5. Stores capability metadata in memory

Safety guarantees:
  - Max 5 evolutions/day, 2/hour (Redis counters, fail-open on Redis error)
  - Sandbox: AST parse + security blocklist + structural checks
  - Generated skills always registered at SAFE capability level
  - Never overwrites existing skills
  - Full structlog audit trail + append-only log file at /data/logs/capability_evolution.log
  - All failures are silent (never crashes main loop)

Integration:
  - GoalOrchestrator.capability_evolution_engine (late-wired from main.py)
  - Triggered fire-and-forget after goal failure, same pattern as reflection_engine
  - Periodic scan via CapabilityEvolutionJob (every 3600s)

Gap score formula:
  gap_score = 0.6 × repeated_failures_signal
            + 0.2 × error_keyword_signal
            + 0.2 × reflection_gap_signal
  Threshold: 0.50
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import redis.asyncio as aioredis
import structlog

from .models.manager import ModelManager
from .models.types import Message, ModelRequest
from .skills.registry import SkillRegistry

if TYPE_CHECKING:
    from .memory.manager import MemoryManager

logger = structlog.get_logger()

# ── Tunable constants ──────────────────────────────────────────────────────────
_MAX_PER_DAY     = 5          # Max capability evolutions per calendar day
_MAX_PER_HOUR    = 2          # Max capability evolutions per hour
_THRESHOLD       = 0.50       # gap_score must exceed this to attempt evolution
_GEN_DIR         = "/data/skills/generated"   # Where generated skills live
_LOG_FILE        = "/data/logs/capability_evolution.log"

# Keywords in reflection text that signal a capability gap
_REFLECTION_GAP_KW = frozenset([
    "no tool", "missing tool", "no skill", "missing skill",
    "unable to", "can't", "cannot", "not available", "not found",
    "no capability", "doesn't have", "need a tool", "would need",
    "no herramienta", "falta herramienta", "sin capacidad",
    "no puedo", "imposible", "no disponible", "lacking",
])

# Keywords in error text that signal a missing tool or capability
_ERROR_GAP_KW = frozenset([
    "importerror", "modulenotfounderror", "attributeerror", "nameerror",
    "notimplementederror", "skill not found", "no skill registered",
    "not registered", "disabled", "not enabled",
])

# Patterns that must NOT appear in generated code (security blocklist)
_BLOCKED_PATTERNS = [
    "os.system(", "os.popen(", "os.exec",
    "import subprocess", "subprocess.", "__import__(", "eval(", "exec(",
    "open(", "socket.", "urllib.request", "requests.",
    "httpx.", "ctypes.", "importlib.import_module",
]

# ── LLM system prompt for code generation ─────────────────────────────────────
_GEN_SYSTEM_PROMPT = """\
You are a Python skill generator for the WASP autonomous AI agent.
Generate a minimal, safe skill class for a missing capability.

STRICT RULES:
- Class name must be exactly: GeneratedSkill
- Only use Python standard library (math, re, json, datetime, hashlib, base64, etc.)
- Do NOT use: os.system, subprocess, requests, httpx, urllib, eval, exec, socket, open
- execute() must be async, accept **kwargs, return SkillResult(skill_name=..., success=..., output=...)
- Keep under 65 lines total

EXACT TEMPLATE (replace {skill_name} and {description} and fill execute() body):

from src.skills.base import SkillBase
from src.skills.types import SkillDefinition, SkillResult

class GeneratedSkill(SkillBase):
    name = "{skill_name}"

    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name=self.name,
            description="{description}",
            params=[],
            category="generated",
            capability_level="safe",
            timeout_seconds=10.0,
        )

    async def execute(self, **kwargs) -> SkillResult:
        try:
            result = "computed result here"
            return SkillResult(skill_name=self.name, success=True, output=result)
        except Exception as e:
            return SkillResult(skill_name=self.name, success=False, output="", error=str(e))

Respond with ONLY the Python code. No markdown, no explanation, no code fences."""


class CapabilityEvolutionEngine:
    """Detects capability gaps, evolves new skills, and registers them in SkillRegistry."""

    def __init__(
        self,
        model_manager: ModelManager,
        skill_registry: SkillRegistry,
        redis_url: str = "",
        memory_manager: "MemoryManager | None" = None,
    ):
        self.model_manager = model_manager
        self.skill_registry = skill_registry
        self.redis_url = redis_url
        self.memory_manager = memory_manager
        # Late-wired from main.py (matches reflection_engine / governor pattern)
        self.reflection_engine = None
        self.governor = None

    # ── Main Entry Point ───────────────────────────────────────────────────────

    async def analyze_gap(
        self,
        goal_id: str,
        objective: str,
        error: str = "",
        outcome: str = "failure",
        consecutive_failures: int = 0,
    ) -> bool:
        """Analyze a failed goal for capability gaps. Fire-and-forget safe.

        Returns True if a new capability was successfully evolved and registered.
        Never raises — all errors are silently logged.
        """
        if outcome != "failure":
            return False

        try:
            # Rate-limit check (cheap Redis reads)
            if await self._is_rate_limited():
                logger.debug("cee.rate_limited")
                return False

            # Compute gap score from three signals
            gap_score, cap_name, description = await self._compute_gap_score(
                goal_id, objective, error, consecutive_failures
            )

            if gap_score < _THRESHOLD or not cap_name:
                logger.debug(
                    "cee.below_threshold",
                    score=round(gap_score, 2),
                    threshold=_THRESHOLD,
                    name=cap_name or "<no-name>",
                )
                return False

            # Dedup — skip if skill already registered
            if self.skill_registry.get(cap_name) is not None:
                logger.debug("cee.skill_already_exists", name=cap_name)
                return False

            logger.info(
                "cee.gap_detected",
                goal_id=goal_id[:8],
                capability_name=cap_name,
                gap_score=round(gap_score, 2),
            )

            # Generate candidate code via LLM
            code = await self._generate_code(cap_name, description, objective, error)
            if not code:
                await self._log_event(cap_name, "code_generation_failed", "generation", "skipped")
                return False

            # Validate in sandbox (no subprocess needed — static analysis is safe enough)
            valid, reason = self._validate_code(code, cap_name)
            if not valid:
                logger.warning("cee.validation_failed", name=cap_name, reason=reason)
                await self._log_event(cap_name, f"validation_failed:{reason}", "validation", "rejected")
                return False

            # Register the new skill dynamically
            registered = await self._register_skill(cap_name, description, code)
            if not registered:
                await self._log_event(cap_name, "registration_failed", "registration", "failed")
                return False

            # Increment rate-limit counters
            await self._record_evolution()

            # Persist in memory so future context knows this skill exists
            await self._store_memory(cap_name, objective, gap_score)

            await self._log_event(
                cap_name, f"gap_score={gap_score:.2f},goal={goal_id[:8]}", "complete", "success"
            )
            logger.info(
                "cee.capability_evolved",
                capability_name=cap_name,
                gap_score=round(gap_score, 2),
                goal_id=goal_id[:8],
            )
            return True

        except Exception as exc:
            logger.debug("cee.analyze_gap_error", error=str(exc)[:200])
            return False

    # ── Gap Score Computation ──────────────────────────────────────────────────

    async def _compute_gap_score(
        self,
        goal_id: str,
        objective: str,
        error: str,
        consecutive_failures: int,
    ) -> tuple[float, str, str]:
        """Return (score 0-1, capability_name, description).

        gap_score = 0.6×failures_signal + 0.2×error_signal + 0.2×reflection_signal
        """
        # Signal 1: repeated failures (weight 0.6) — 3+ failures = max signal
        fail_signal = min(1.0, consecutive_failures / 3.0)

        # Signal 2: error text keywords (weight 0.2)
        err_lower = (error or "").lower()
        err_signal = float(any(kw in err_lower for kw in _ERROR_GAP_KW))

        # Signal 3: reflection gap keywords (weight 0.2)
        ref_signal = 0.0
        if self.reflection_engine:
            try:
                refs = await self.reflection_engine.get_reflections_for_goal(goal_id)
                combined = " ".join(r.get("reflection", "") for r in refs).lower()
                ref_signal = float(any(kw in combined for kw in _REFLECTION_GAP_KW))
            except Exception:
                pass

        score = round(0.6 * fail_signal + 0.2 * err_signal + 0.2 * ref_signal, 3)
        cap_name, description = self._extract_capability_name(objective, error)
        return score, cap_name, description

    def _extract_capability_name(self, objective: str, error: str) -> tuple[str, str]:
        """Derive a snake_case skill name and description from context."""
        import re

        # Pattern: "skill not found: foo_bar" or "not registered: foo_bar"
        for pat in [
            r"skill[_\s]+not[_\s]+found[:\s]+([a-z_][a-z0-9_]*)",
            r"not[_\s]+registered[:\s]+([a-z_][a-z0-9_]*)",
        ]:
            m = re.search(pat, (error or "").lower())
            if m:
                name = m.group(1).strip().replace(" ", "_")[:40]
                return name, f"Auto-generated skill for: {objective[:80]}"

        # Derive from the most meaningful words in the objective
        words = re.findall(r"[a-z]+", objective.lower())
        _stop = {"the", "a", "an", "to", "of", "and", "or", "in", "for", "with",
                 "que", "de", "la", "el", "en", "y", "un", "una", "los"}
        meaningful = [w for w in words if len(w) > 3 and w not in _stop]
        if len(meaningful) >= 2:
            name = "_".join(meaningful[:3])[:40]
            return name, f"Auto-generated skill for: {objective[:80]}"

        return "", ""

    # ── Code Generation ────────────────────────────────────────────────────────

    async def _generate_code(
        self,
        cap_name: str,
        description: str,
        objective: str,
        error: str,
    ) -> str:
        """Ask LLM to generate minimal skill code. Returns empty string on failure."""
        if not self.model_manager:
            return ""

        # Governor gate — count this LLM call against the global rate limit.
        # CEE's own Redis hourly/daily caps are checked separately in analyze_gap().
        # Both layers must pass before any code is generated.
        if self.governor:
            try:
                allowed, reason = await self.governor.check_allow("llm_call", "system")
                if not allowed:
                    logger.info("cee.governor_blocked", reason=reason, cap_name=cap_name)
                    return ""
            except Exception as _gov_exc:
                # Governor failure is non-fatal — fail open (matching governor's own policy)
                logger.debug("cee.governor_check_failed", error=str(_gov_exc)[:120])

        prompt = (
            f"Missing capability: {cap_name}\n"
            f"Description: {description}\n"
            f"Failed objective: {objective[:200]}\n"
            f"Error: {(error or 'unknown')[:200]}\n\n"
            f"Set name = '{cap_name}' in the template.\n"
            "Implement execute() body with pure Python computation only.\n"
            "No network calls, no file I/O, no subprocess, no shell."
        )

        try:
            req = ModelRequest(
                messages=[
                    Message(role="system", content=_GEN_SYSTEM_PROMPT),
                    Message(role="user", content=prompt),
                ],
                max_tokens=500,
            )
            resp = await self.model_manager.generate(req)
            code = (resp.content or "").strip()

            # Strip accidental markdown code fences
            for fence in ("```python", "```"):
                if code.startswith(fence):
                    code = code[len(fence):]
            if code.endswith("```"):
                code = code[:-3]
            return code.strip()

        except Exception as exc:
            logger.debug("cee.llm_failed", error=str(exc)[:120])
            return ""

    # ── Sandbox Validation ─────────────────────────────────────────────────────

    def _validate_code(self, code: str, cap_name: str) -> tuple[bool, str]:
        """Validate generated code without executing it.

        Checks:
          1. AST syntax parse
          2. Security blocklist (no shell, subprocess, eval, etc.)
          3. Required structural elements (class + methods + SkillResult)
          4. Skill name appears in code
        """
        # 1. Syntax via AST
        try:
            ast.parse(code)
        except SyntaxError as e:
            return False, f"SyntaxError: {e}"

        # 2. Security blocklist
        for blocked in _BLOCKED_PATTERNS:
            if blocked in code:
                return False, f"blocked_pattern: {blocked}"

        # 3. Required structural elements
        if "class GeneratedSkill" not in code:
            return False, "missing class GeneratedSkill"
        if "def definition" not in code:
            return False, "missing definition() method"
        if "async def execute" not in code:
            return False, "missing async execute() method"
        if "SkillResult" not in code:
            return False, "missing SkillResult return"
        if "SkillDefinition" not in code:
            return False, "missing SkillDefinition"

        # 4. Skill name must appear as a string literal in the code
        if f'"{cap_name}"' not in code and f"'{cap_name}'" not in code:
            return False, f"skill name '{cap_name}' not found as string literal in code"

        return True, "ok"

    # ── Dynamic Skill Registration ─────────────────────────────────────────────

    async def _register_skill(
        self,
        cap_name: str,
        description: str,
        code: str,
    ) -> bool:
        """Write skill file, load dynamically, register in SkillRegistry."""
        try:
            skill_dir = os.path.join(_GEN_DIR, cap_name)
            os.makedirs(skill_dir, exist_ok=True)
            skill_path = os.path.join(skill_dir, "skill.py")

            # Never overwrite an existing skill file
            if os.path.exists(skill_path):
                logger.debug("cee.skill_file_exists", path=skill_path)
                return False

            with open(skill_path, "w") as f:
                f.write(
                    f"# Auto-generated by CapabilityEvolutionEngine\n"
                    f"# Description: {description}\n"
                    f"# Generated: {datetime.now(timezone.utc).isoformat()}\n\n"
                )
                f.write(code)

            # Ensure /app is importable (needed for src.skills.base etc.)
            if "/app" not in sys.path:
                sys.path.insert(0, "/app")

            # Load the module dynamically
            spec = importlib.util.spec_from_file_location(
                f"cee_gen.{cap_name}", skill_path
            )
            if spec is None or spec.loader is None:
                logger.warning("cee.spec_failed", path=skill_path)
                return False

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)  # type: ignore[union-attr]

            skill_class = getattr(module, "GeneratedSkill", None)
            if skill_class is None:
                logger.warning("cee.class_not_found", path=skill_path)
                return False

            skill_instance = skill_class()
            self.skill_registry.register(skill_instance)

            # Register at SAFE capability level (generated tools are always least-privilege)
            try:
                from .skills.capability import CapabilityLevel, capability_registry
                capability_registry.register(cap_name, CapabilityLevel.SAFE)
            except Exception:
                pass  # Non-fatal — skill is registered even if capability level fails

            logger.info("cee.skill_registered", name=cap_name, path=skill_path)
            return True

        except Exception as exc:
            logger.warning("cee.register_error", name=cap_name, error=str(exc)[:200])
            return False

    # ── Memory Storage ─────────────────────────────────────────────────────────

    async def _store_memory(
        self,
        cap_name: str,
        objective: str,
        gap_score: float,
    ) -> None:
        """Persist capability evolution metadata in agent memory (MemoryType.META)."""
        if not self.memory_manager:
            return
        try:
            from .db.session import async_session
            from .memory.types import MemoryType
            async with async_session() as session:
                await self.memory_manager.store_memory(
                    session=session,
                    memory_type=MemoryType.META,
                    content={
                        "capability_name": cap_name,
                        "gap_score": round(gap_score, 2),
                        "task_context": objective[:200],
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "source": "capability_evolution_engine",
                    },
                    summary=f"Evolved skill: {cap_name} (gap_score={gap_score:.2f})",
                    tags=["generated_skill", "capability_evolution", cap_name],
                    importance=0.7,
                )
        except Exception as exc:
            logger.debug("cee.memory_failed", error=str(exc)[:120])

    # ── Rate Limiting ──────────────────────────────────────────────────────────

    async def _is_rate_limited(self) -> bool:
        """Check daily/hourly caps. Returns False (allow) on any Redis error."""
        if not self.redis_url:
            return False
        try:
            r = aioredis.from_url(self.redis_url, decode_responses=True)
            try:
                now = time.time()
                daily  = int(await r.get(f"cee:daily:{int(now // 86400)}") or 0)
                hourly = int(await r.get(f"cee:hourly:{int(now // 3600)}") or 0)
                return daily >= _MAX_PER_DAY or hourly >= _MAX_PER_HOUR
            finally:
                await r.aclose()
        except Exception:
            return False  # Fail open — never block on infrastructure failure

    async def _record_evolution(self) -> None:
        """Increment daily and hourly evolution counters after success."""
        if not self.redis_url:
            return
        try:
            r = aioredis.from_url(self.redis_url, decode_responses=True)
            try:
                now = time.time()
                dk = f"cee:daily:{int(now // 86400)}"
                hk = f"cee:hourly:{int(now // 3600)}"
                pipe = r.pipeline()
                pipe.incr(dk); pipe.expire(dk, 86400)
                pipe.incr(hk); pipe.expire(hk, 3600)
                await pipe.execute()
            finally:
                await r.aclose()
        except Exception:
            pass

    # ── Logging ────────────────────────────────────────────────────────────────

    async def _log_event(
        self,
        cap_name: str,
        trigger_reason: str,
        phase: str,
        result: str,
    ) -> None:
        """Append a JSON entry to /data/logs/capability_evolution.log."""
        try:
            os.makedirs(os.path.dirname(_LOG_FILE), exist_ok=True)
            entry = json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "capability_name": cap_name,
                "trigger_reason": trigger_reason[:200],
                "phase": phase,
                "result": result,
            })
            with open(_LOG_FILE, "a") as f:
                f.write(entry + "\n")
        except Exception:
            pass  # Log file is best-effort; never crash on log failure
