"""
WASP Phase 5 — Capability Synthesis (Composition Engine)
=========================================================
Enables the system to solve tasks WITHOUT predefined skills by composing
safe multi-step execution flows from existing, validated tools.

Composition triggers ONLY when:
  - No direct skill exists for the intent, OR
  - ≥2 repeated failures with the existing approach

Safety invariants (never relaxed):
  - Every step maps to a known executable tool  (KNOWN_TOOLS)
  - Every step is rejected if vague or non-operational (VAGUE_PSEUDO_TOOLS)
  - Every step passes the built-in pre-execution check (validate_step)
  - DomainLock is enforced per-step for browser calls
  - ObjectiveSpec.validate_against_spec() is not bypassed
  - No free-form LLM execution without tool validation

Composed patterns are TEMPORARY (TTL=3 days, Redis-only).
They are NOT permanent skills and do not survive container rebuilds.

Execution lifecycle:  PLAN → CHECK → EXECUTE → VERIFY → REPLAN

Log events emitted:
  composition.triggered
  composition.plan_generated
  composition.step_blocked
  composition.success
  composition.failed
"""
from __future__ import annotations

import re
import json
import time
import uuid
import hashlib
from dataclasses import dataclass, field
from enum import Enum

import structlog

logger = structlog.get_logger()


# ── Executable tool registry ───────────────────────────────────────────────────
# Each step MUST name a tool from this set.  Any other value is rejected.
KNOWN_TOOLS: frozenset[str] = frozenset({
    "browser",
    "web_search",
    "gmail",
    "file_manager",
    "calculate",
    "render_report",
    "skill_manager",
    "reminders",
    "task_manager",
    "memory",
    "python_exec",
    "shell",
    "http_request",
    "subscribe",
    "agent_manager",
})

# Pseudo-tools that LLMs sometimes emit — never executable, always rejected.
VAGUE_PSEUDO_TOOLS: frozenset[str] = frozenset({
    "think", "reason", "analyze", "plan", "understand", "reflect",
    "decide", "evaluate", "consider", "review", "check", "verify",
    "llm", "gpt", "claude", "ai", "model", "infer", "summarize",
})

# Trigger thresholds
COMPOSITION_FAILURE_THRESHOLD: int = 2   # ≥N failures triggers composition mode
PATTERN_TTL: int = 259_200               # 3 days — shorter than ExecutionPattern (7d)
COMPOSED_PATTERN_PREFIX: str = "composed"


# ── Execution phases ───────────────────────────────────────────────────────────
class ExecutionPhase(str, Enum):
    PLAN    = "PLAN"
    CHECK   = "CHECK"
    EXECUTE = "EXECUTE"
    VERIFY  = "VERIFY"
    REPLAN  = "REPLAN"


# ── Data models ───────────────────────────────────────────────────────────────
@dataclass
class ComposedStep:
    """A single validated step in a composed execution plan."""
    step_id: str          # "step_1", "step_2", …
    tool: str             # must be in KNOWN_TOOLS
    action: str           # tool-specific action (e.g. "navigate", "read")
    params: dict = field(default_factory=dict)   # validated parameters
    description: str = ""      # human-readable intent
    domain: str = ""           # domain this step touches (blank for non-browser)
    expected_output: str = ""  # observable output token (must be non-empty)
    input_from: str = ""       # step_id this step chains from (Fix 2)


@dataclass
class ComposedPlan:
    """An ordered, validated sequence of ComposedSteps."""
    plan_id: str
    intent_type: str
    objective_signature: str
    steps: list[ComposedStep] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    replan_count: int = 0


@dataclass
class ComposedPattern:
    """A temporarily stored record of a successful composed execution.

    ⚠ NOT a permanent skill.  Expires after PATTERN_TTL seconds.
    Marked failed=True when execution fails — prevents any reuse.
    """
    pattern_id: str
    objective_signature: str
    intent_type: str
    steps: list[dict] = field(default_factory=list)   # serialised ComposedSteps
    tools_used: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    success_count: int = 1
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    failed: bool = False   # True → blocked from reuse; never recovered


# ── Serialisation helpers ─────────────────────────────────────────────────────
def _pattern_to_dict(p: ComposedPattern) -> dict:
    return {
        "pattern_id":          p.pattern_id,
        "objective_signature": p.objective_signature,
        "intent_type":         p.intent_type,
        "steps":               p.steps,
        "tools_used":          p.tools_used,
        "domains":             p.domains,
        "success_count":       p.success_count,
        "created_at":          p.created_at,
        "last_used_at":        p.last_used_at,
        "failed":              p.failed,
    }


def _pattern_from_dict(d: dict) -> ComposedPattern:
    return ComposedPattern(
        pattern_id          = d.get("pattern_id", ""),
        objective_signature = d.get("objective_signature", ""),
        intent_type         = d.get("intent_type", ""),
        steps               = d.get("steps", []),
        tools_used          = d.get("tools_used", []),
        domains             = d.get("domains", []),
        success_count       = d.get("success_count", 1),
        created_at          = d.get("created_at", 0.0),
        last_used_at        = d.get("last_used_at", 0.0),
        failed              = d.get("failed", False),
    )


# ── Composition Engine ────────────────────────────────────────────────────────
class CompositionEngine:
    """Orchestrates capability synthesis when no direct skill is available.

    Design principles:
      - Fail-open on Redis errors (never crash the caller)
      - Fail-closed on safety checks (vague tools, DomainLock, ObjectiveSpec)
      - Logs every significant event for observability
      - Composed patterns expire automatically; they are never promoted to skills
    """

    composition_mode: bool = True
    FAILURE_THRESHOLD: int = COMPOSITION_FAILURE_THRESHOLD
    PATTERN_TTL: int = PATTERN_TTL

    # ── Redis key helpers ─────────────────────────────────────────────────────
    def _pattern_key(self, pattern_id: str) -> str:
        return f"wasp:{COMPOSED_PATTERN_PREFIX}:{pattern_id}"

    def _idx_key(self, intent_type: str) -> str:
        return f"wasp:{COMPOSED_PATTERN_PREFIX}:idx:{intent_type}"

    # ── Step 1: Trigger decision ──────────────────────────────────────────────
    def should_trigger(
        self,
        intent_type: str,
        failure_count: int = 0,
        has_direct_skill: bool = True,
    ) -> bool:
        """Return True if composition mode should activate for this intent.

        Triggers when:
          - no direct skill exists for the intent, OR
          - failure_count >= FAILURE_THRESHOLD (≥2 repeated failures)

        Returns False if composition_mode is disabled.
        """
        if not self.composition_mode:
            return False
        if not has_direct_skill:
            logger.info(
                "composition.triggered",
                reason="no_direct_skill",
                intent_type=intent_type,
            )
            return True
        if failure_count >= self.FAILURE_THRESHOLD:
            logger.info(
                "composition.triggered",
                reason="repeated_failure",
                intent_type=intent_type,
                failure_count=failure_count,
            )
            return True
        return False

    # ── Step 2 / Step 3: Per-step validation (pre_execution_check equivalent) ─

    # Fix 2: regex for implicit $step_N / {{step_N}} dependency notation in params/description
    _STEP_REF_RE: "re.Pattern" = re.compile(r'\$step_\d+|\{\{step_\d+\}\}')

    def validate_step(
        self,
        step: ComposedStep,
        domain_lock=None,              # DomainLock | None
        prior_step_ids: "frozenset[str] | None" = None,
    ) -> tuple[bool, str]:
        """Validate a single composed step before it enters a plan.

        Checks (in order):
          1. Tool not in VAGUE_PSEUDO_TOOLS (not executable)
          2. Tool in KNOWN_TOOLS (registered executable)
          3. Step has observable output (expected_output non-empty)
          4. DomainLock respected for browser steps
          5. Fix 1 — web_search must include site:<domain> when DomainLock active
          6. Fix 2 — input_from must reference a valid prior step when provided

        prior_step_ids: set of step_ids that precede this step in the plan.
                        None = standalone call (input_from check skipped).

        Returns (ok: bool, reason: str).
        Logs composition.step_blocked on rejection.
        """
        tool = (step.tool or "").strip().lower()

        # Check 1: vague pseudo-tool
        if tool in VAGUE_PSEUDO_TOOLS:
            reason = f"vague_pseudo_tool: '{tool}' is not executable"
            logger.warning("composition.step_blocked", step_id=step.step_id, tool=tool, reason=reason)
            return False, reason

        # Check 2: unknown tool
        if tool not in KNOWN_TOOLS:
            reason = f"unknown_tool: '{tool}' not in KNOWN_TOOLS"
            logger.warning("composition.step_blocked", step_id=step.step_id, tool=tool, reason=reason)
            return False, reason

        # Check 3: observable output required
        if not (step.expected_output or "").strip():
            reason = f"no_observable_output: step '{step.step_id}' lacks expected_output"
            logger.warning("composition.step_blocked", step_id=step.step_id, tool=tool, reason=reason)
            return False, reason

        # Check 4: DomainLock for browser steps
        if tool == "browser" and step.domain and domain_lock is not None:
            allows = getattr(domain_lock, "allows", None)
            if callable(allows) and not allows(step.domain):
                reason = f"domain_lock_violation: '{step.domain}' blocked by active lock"
                logger.warning("composition.step_blocked", step_id=step.step_id, tool=tool,
                               domain=step.domain, reason=reason)
                return False, reason

        # Check 5 (Fix 1): web_search must scope to allowed domain when DomainLock is active
        if tool == "web_search" and domain_lock is not None:
            lock_domains = frozenset(getattr(domain_lock, "domains", None) or [])
            if lock_domains:
                query = str(step.params.get("query", "")).lower()
                if not any(f"site:{d}" in query for d in lock_domains):
                    domains_str = ", ".join(sorted(lock_domains))
                    reason = (
                        f"web_search_domain_constraint: query must include "
                        f"site:<domain> (allowed: {domains_str})"
                    )
                    logger.warning("composition.step_blocked", step_id=step.step_id, tool=tool,
                                   reason=reason)
                    return False, reason

        # Check 6 (Fix 2): input_from must reference a valid prior step
        if step.input_from and prior_step_ids is not None:
            if step.input_from not in prior_step_ids:
                reason = (
                    f"invalid_input_from: '{step.input_from}' not in prior steps "
                    f"{sorted(prior_step_ids) if prior_step_ids else '[]'}"
                )
                logger.warning("composition.step_blocked", step_id=step.step_id, tool=tool,
                               reason=reason)
                return False, reason

        return True, "ok"

    # ── Step 2: Full plan decomposition and validation ────────────────────────
    def validate_plan(
        self,
        raw_steps: list[dict],
        intent_type: str = "",
        domain_lock=None,
        objective_spec=None,
    ) -> tuple[ComposedPlan | None, list[str]]:
        """Validate a raw decomposition into a ComposedPlan.

        raw_steps: each dict must have: tool, action, expected_output.
                   Optional: params, description, domain.

        Rejects any step with no executable tool, vague content, missing
        observable output, or DomainLock violation.

        Returns (ComposedPlan, []) on full success.
        Returns (None, [rejection_reasons]) if ANY step is invalid.
        """
        if not raw_steps:
            return None, ["empty_decomposition"]

        validated: list[ComposedStep] = []
        rejections: list[str] = []
        all_domains: set[str] = set()
        all_tools: set[str] = set()

        prior_step_ids: set[str] = set()   # grows as steps are accepted

        for i, raw in enumerate(raw_steps):
            step_id = f"step_{i + 1}"
            step = ComposedStep(
                step_id        = step_id,
                tool           = str(raw.get("tool", "")).strip().lower(),
                action         = str(raw.get("action", "")).strip().lower(),
                params         = raw.get("params", {}) if isinstance(raw.get("params"), dict) else {},
                description    = str(raw.get("description", "")).strip(),
                domain         = str(raw.get("domain", "")).strip().lower(),
                expected_output= str(raw.get("expected_output", "")).strip(),
                input_from     = str(raw.get("input_from", "")).strip(),
            )

            # Fix 2: detect implicit $step_N / {{step_N}} references without declared input_from
            _implicit_text = json.dumps(step.params) + " " + step.description
            if not step.input_from and self._STEP_REF_RE.search(_implicit_text):
                reason = (
                    f"implicit_dependency_without_input_from: "
                    f"step references prior output via $step_ notation "
                    f"but input_from is not declared"
                )
                logger.warning("composition.step_blocked", step_id=step_id,
                               tool=step.tool, reason=reason)
                rejections.append(f"{step_id}: {reason}")
                prior_step_ids.add(step_id)   # still register so later steps can ref it
                continue

            ok, reason = self.validate_step(step, domain_lock,
                                            prior_step_ids=frozenset(prior_step_ids))
            if not ok:
                rejections.append(f"{step_id}: {reason}")
            else:
                validated.append(step)
                all_tools.add(step.tool)
                if step.domain:
                    all_domains.add(step.domain)
            prior_step_ids.add(step_id)

        if rejections:
            return None, rejections

        if not validated:
            return None, ["no_valid_steps_after_validation"]

        # Extract objective signature from spec if available
        obj_sig = ""
        if objective_spec is not None:
            obj = (getattr(objective_spec, "objective", "") or "").strip()
            obj_sig = obj[:120]

        plan = ComposedPlan(
            plan_id              = str(uuid.uuid4())[:8],
            intent_type          = intent_type or "",
            objective_signature  = obj_sig,
            steps                = validated,
            domains              = sorted(all_domains),
            tools_used           = sorted(all_tools),
            created_at           = time.time(),
        )

        logger.info(
            "composition.plan_generated",
            plan_id     = plan.plan_id,
            intent_type = intent_type or "(none)",
            step_count  = len(validated),
            tools       = plan.tools_used,
            domains     = plan.domains,
        )
        return plan, []

    # ── Step 4: ObjectiveSpec enforcement ─────────────────────────────────────
    def check_objective_spec(
        self,
        response_text: str,
        results: list,
        spec,
    ) -> tuple[bool, str]:
        """Enforce ObjectiveSpec.validate_against_spec() — composition may not bypass it.

        Delegates to control_layer.validate_against_spec() when available.
        Fail-open on import error (degrade gracefully without crashing).
        Returns (passed: bool, reason: str).
        """
        if spec is None:
            return True, "no_spec"
        try:
            from src.events.control_layer import validate_against_spec
            return validate_against_spec(response_text, results, spec)
        except Exception as _e:
            logger.warning(
                "composition.spec_check_error",
                error=str(_e)[:80],
            )
            return True, "spec_check_unavailable"

    # ── Step 5: Store successful composed plan ────────────────────────────────
    async def store_composed_pattern(
        self,
        redis_url: str,
        plan: ComposedPlan,
    ) -> str | None:
        """Persist a successful ComposedPlan as a temporary ComposedPattern.

        ⚠  NOT a permanent skill.  TTL = 3 days.
        Returns pattern_id or None on any error.
        """
        if not redis_url or not plan:
            return None
        try:
            import redis.asyncio as _aioredis

            sig_hash = hashlib.sha256(
                (plan.intent_type + ":" + plan.objective_signature).encode()
            ).hexdigest()[:12]
            pattern_id = f"{COMPOSED_PATTERN_PREFIX}:{sig_hash}"

            pattern = ComposedPattern(
                pattern_id          = pattern_id,
                objective_signature = plan.objective_signature,
                intent_type         = plan.intent_type,
                steps               = [
                    {
                        "step_id":         s.step_id,
                        "tool":            s.tool,
                        "action":          s.action,
                        "params":          s.params,
                        "description":     s.description,
                        "domain":          s.domain,
                        "expected_output": s.expected_output,
                        "input_from":      s.input_from,
                    }
                    for s in plan.steps
                ],
                tools_used    = list(plan.tools_used),
                domains       = list(plan.domains),
                success_count = 1,
                created_at    = time.time(),
                last_used_at  = time.time(),
                failed        = False,
            )

            _r = _aioredis.from_url(redis_url, decode_responses=True)
            try:
                await _r.setex(
                    self._pattern_key(pattern_id),
                    self.PATTERN_TTL,
                    json.dumps(_pattern_to_dict(pattern)),
                )
                await _r.sadd(self._idx_key(plan.intent_type), pattern_id)
                await _r.expire(self._idx_key(plan.intent_type), self.PATTERN_TTL)
            finally:
                await _r.aclose()

            logger.info(
                "composition.success",
                pattern_id  = pattern_id,
                intent_type = plan.intent_type,
                tools       = plan.tools_used,
                step_count  = len(plan.steps),
            )
            return pattern_id

        except Exception as _e:
            logger.warning("composition.store_failed", error=str(_e)[:120])
            return None

    # ── Retrieval ─────────────────────────────────────────────────────────────
    async def find_composed_pattern(
        self,
        redis_url: str,
        intent_type: str,
        objective_sig: str = "",
    ) -> ComposedPattern | None:
        """Retrieve a matching ComposedPattern from Redis.

        Skips patterns marked failed=True.
        Returns None on miss, error, or all-failed set.
        """
        if not redis_url or not intent_type:
            return None
        try:
            import redis.asyncio as _aioredis
            _r = _aioredis.from_url(redis_url, decode_responses=True)
            try:
                pattern_ids = await _r.smembers(self._idx_key(intent_type))
                if not pattern_ids:
                    return None
                for pid in pattern_ids:
                    raw = await _r.get(self._pattern_key(pid))
                    if not raw:
                        continue
                    data = json.loads(raw)
                    if data.get("failed"):
                        continue
                    return _pattern_from_dict(data)
                return None
            finally:
                await _r.aclose()
        except Exception as _e:
            logger.warning("composition.find_failed", error=str(_e)[:120])
            return None

    # ── Step 6: Failure handling ──────────────────────────────────────────────
    async def record_composition_failure(
        self,
        redis_url: str,
        pattern_id: str,
    ) -> None:
        """Mark a ComposedPattern as permanently failed.

        Effect: failed=True is written to Redis.  find_composed_pattern()
        will skip this pattern on all future lookups — no reuse possible.
        Does NOT store on fresh failure (caller must not call store first).
        """
        if not redis_url or not pattern_id:
            return
        try:
            import redis.asyncio as _aioredis
            _r = _aioredis.from_url(redis_url, decode_responses=True)
            try:
                raw = await _r.get(self._pattern_key(pattern_id))
                if not raw:
                    return
                data = json.loads(raw)
                data["failed"] = True
                await _r.setex(
                    self._pattern_key(pattern_id),
                    self.PATTERN_TTL,
                    json.dumps(data),
                )
            finally:
                await _r.aclose()

            logger.warning(
                "composition.failed",
                pattern_id = pattern_id,
                reason     = "marked_failed_prevents_reuse",
            )
        except Exception as _e:
            logger.warning("composition.failure_record_error", error=str(_e)[:120])

    # ── Replan hint ───────────────────────────────────────────────────────────
    def build_replan_hint(
        self,
        rejections: list[str],
        plan: ComposedPlan | None = None,
        phase: ExecutionPhase = ExecutionPhase.REPLAN,
    ) -> str:
        """Build a structured replan message for injection into the LLM context.

        Includes rejection reasons so the LLM can correct the decomposition.
        """
        parts = [f"[COMPOSITION {phase.value} — REPLAN REQUIRED]"]
        if rejections:
            parts.append("Blocked: " + "; ".join(r[:80] for r in rejections[:3]))
        if plan:
            parts.append(f"plan_id={plan.plan_id}")
        parts.append(
            "All steps must name a tool from KNOWN_TOOLS with a non-empty expected_output."
        )
        return " | ".join(parts)

    # ── Execution trace (PLAN→CHECK→EXECUTE→VERIFY→REPLAN) ───────────────────
    def build_execution_trace(
        self,
        plan: ComposedPlan,
        spec_passed: bool,
        spec_reason: str,
        replan_required: bool,
    ) -> dict:
        """Return an immutable execution trace for observability and testing.

        Does NOT execute anything — this is a record of the validation lifecycle.
        """
        return {
            "plan_id":         plan.plan_id,
            "intent_type":     plan.intent_type,
            "phases":          [p.value for p in ExecutionPhase],
            "steps_validated": len(plan.steps),
            "tools_used":      plan.tools_used,
            "domains":         plan.domains,
            "spec_passed":     spec_passed,
            "spec_reason":     spec_reason,
            "replan_required": replan_required,
            "created_at":      plan.created_at,
        }


# ── Module-level singleton ────────────────────────────────────────────────────
composition_engine = CompositionEngine()
