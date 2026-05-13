from __future__ import annotations
import json
import re
import time
from dataclasses import dataclass, field

import redis.asyncio as aioredis
import structlog

logger = structlog.get_logger()

MIN_SCORE = 4.0
MIN_LOCAL_SUCCESS_RATE = 0.85      # Phase 2: strict 85% threshold
MIN_COMPLETENESS = 0.85            # Phase 4: required output completeness
CAPABILITY_INDEX_KEY = "capability:index"

_STOPWORDS = frozenset({
    "the", "and", "for", "are", "was", "were", "has", "have", "had",
    "que", "con", "una", "las", "los", "por", "del", "para", "como",
    "esta", "esto", "este",
})

# Phase 1/3: Hard args MUST resolve — abort capability if any is missing
_HARD_ARGS = frozenset({"url", "paths", "action", "to", "from"})

# Phase 1: Explicitly optional args — LLM-generated analysis that can be empty
_OPTIONAL_ARGS = frozenset({
    "comparison", "trend", "risks", "opportunities", "interpretation",
    "analysis", "summary", "notes", "comments",
    "attachments", "cc", "bcc",
    "headers", "timeout",
})

# Phase 4: Required fields that MUST have non-empty values in render_report output
_REQUIRED_RENDER_FIELDS: dict[str, frozenset[str]] = {
    "asset_monitor": frozenset({
        "btc_price", "eth_price", "btc_change", "eth_change",
    }),
}

# Regex to find unfilled {field} placeholders left by _safe_format
_UNFILLED_RE = re.compile(r'\{([a-z_][a-z0-9_]*)\}')


@dataclass
class CapabilityHit:
    capability_name: str
    output: str
    skills_executed: list[str]
    completeness_score: float = 1.0


class CapabilityEngine:
    """Pre-LLM shortcut: execute learned Capabilities directly if intent matches."""

    def __init__(self, redis_url: str, skill_executor) -> None:
        self._redis_url = redis_url
        self._executor = skill_executor

    async def try_execute(self, *, text: str, user_id: str, chat_id: str) -> CapabilityHit | None:
        query_words = _tokenize(text)
        if not query_words:
            return None

        cap_name = await self._match_capability(query_words)
        if not cap_name:
            return None

        capability = await self._load_capability(cap_name)
        if not capability:
            return None

        # Phase 5: skip degraded capabilities
        if capability.get("degraded"):
            logger.info("capability_engine.skipped_degraded", cap=cap_name)
            return None

        # Phase 2: strict success rate gate
        total = capability.get("success_count", 0) + capability.get("failure_count", 0)
        if total > 0:
            rate = capability.get("success_count", 0) / total
            if rate < MIN_LOCAL_SUCCESS_RATE:
                logger.info("capability_engine.rate_too_low", cap=cap_name,
                            rate=round(rate, 2), success=capability.get("success_count"),
                            failure=capability.get("failure_count"))
                return None

        steps = capability.get("steps", [])
        if not steps:
            return None

        has_real_args = any(step.get("args") for step in steps)
        if not has_real_args:
            return None

        # Phase 3: static pre-validation — verify all template dependencies are satisfiable
        pre_ok, pre_reason = _pre_validate(steps)
        if not pre_ok:
            logger.info("capability_engine.pre_validate_failed", cap=cap_name, reason=pre_reason)
            return None

        logger.info("capability_engine.executing", cap=cap_name, steps=len(steps))

        from .execution_context import ExecutionContext
        ctx = ExecutionContext.new(self._redis_url)

        from .types import SkillCall
        skills_executed: list[str] = []
        all_outputs: list[str] = []
        completeness_scores: list[float] = []
        start_ms = int(time.monotonic() * 1000)

        for step_n, step in enumerate(steps):
            skill_name = step["skill"]
            raw_args: dict = dict(step.get("args", {}))

            # Phase 1/3: strict template resolution — hard args abort, optional → empty string
            resolved_args, abort_reason = await self._resolve_args(ctx, step_n, skill_name, raw_args, cap_name)
            if resolved_args is None:
                await self._record_failure(cap_name)
                return None

            # Auto-inject execution_id for extract_fields
            if skill_name == "extract_fields":
                resolved_args["execution_id"] = ctx.execution_id

            call = SkillCall(
                skill_name=skill_name,
                arguments=resolved_args,
                raw_text=f"[capability:{cap_name}:step_{step_n}]",
            )

            try:
                results = await self._executor.execute_batch([call], user_id=user_id, chat_id=chat_id)
                result = results[0] if results else None
            except Exception as exc:
                logger.info("capability_engine.step_error", cap=cap_name, step=step_n, error=str(exc)[:80])
                await self._record_failure(cap_name)
                return None

            if not result or not result.success:
                err = (result.error if result else "no result") or "unknown error"
                logger.info("capability_engine.step_failed", cap=cap_name, step=step_n, error=err[:80])
                await self._record_failure(cap_name)
                return None

            output = result.output or ""
            await ctx.store(f"step_{step_n + 1}_output", output)

            # Phase 4: output completeness validation
            if skill_name == "render_report":
                report_type = resolved_args.get("type", "")
                comp_score, missing = _check_render_completeness(output, report_type, resolved_args)
                completeness_scores.append(comp_score)
                if missing:
                    logger.warning("capability_engine.incomplete_render",
                                   cap=cap_name, missing=missing, score=round(comp_score, 2))
                    if comp_score < MIN_COMPLETENESS:
                        logger.warning("capability_engine.blocked_incomplete_report",
                                       cap=cap_name, missing=missing)
                        await self._record_failure(cap_name)
                        return None

            elif skill_name == "gmail":
                body = resolved_args.get("body", "")
                if not _validate_gmail_body(body):
                    logger.warning("capability_engine.blocked_empty_email", cap=cap_name,
                                   body_len=len(body))
                    await self._record_failure(cap_name)
                    return None

            skills_executed.append(skill_name)
            all_outputs.append(f"[{skill_name}] {output}")

        total_ms = int(time.monotonic() * 1000) - start_ms
        avg_completeness = (sum(completeness_scores) / len(completeness_scores)) if completeness_scores else 1.0

        await self._record_hit(cap_name, total_ms, avg_completeness)
        logger.info("capability_engine.hit", capability=cap_name, steps=len(skills_executed),
                    completeness=round(avg_completeness, 2), latency_ms=total_ms)

        return CapabilityHit(
            capability_name=cap_name,
            output="\n".join(all_outputs),
            skills_executed=skills_executed,
            completeness_score=avg_completeness,
        )

    async def _resolve_args(
        self,
        ctx,
        step_n: int,
        skill_name: str,
        raw_args: dict,
        cap_name: str,
    ) -> tuple[dict | None, str]:
        """
        Phase 1/3: Strict template resolution.
        - Hard args (_HARD_ARGS) → must resolve or abort
        - Optional args (_OPTIONAL_ARGS) → empty string if unresolved
        - All other args → treated as required (conservative)
        """
        resolved: dict = {}
        for k, v in raw_args.items():
            v_str = str(v)
            if not ctx.has_templates(v_str):
                resolved[k] = v_str
                continue

            resolved_v, ok = await ctx.resolve(v_str)
            if ok:
                resolved[k] = resolved_v
                continue

            # Template unresolved — classify and decide
            is_optional = k in _OPTIONAL_ARGS
            if is_optional:
                logger.debug("capability_engine.optional_arg_empty",
                             cap=cap_name, step=step_n, arg=k)
                resolved[k] = ""
            else:
                # Required arg (hard or unknown) — abort
                logger.warning("capability_engine.required_arg_missing",
                               cap=cap_name, step=step_n, skill=skill_name, arg=k)
                return None, f"step {step_n} ({skill_name}): required arg '{k}' unresolved"

        return resolved, "ok"

    async def _match_capability(self, query_words: set[str]) -> str | None:
        r = aioredis.from_url(self._redis_url, decode_responses=True)
        try:
            index = await r.hgetall(CAPABILITY_INDEX_KEY)
        finally:
            await r.aclose()

        if not index:
            return None

        hit_counts: dict[str, int] = {}
        for keyword, cap_name in index.items():
            if keyword.lower() in query_words:
                hit_counts[cap_name] = hit_counts.get(cap_name, 0) + 1

        if not hit_counts:
            return None

        cap_names = list(hit_counts.keys())
        r = aioredis.from_url(self._redis_url, decode_responses=True)
        try:
            raws = await r.mget([f"capability:{n}" for n in cap_names])
        finally:
            await r.aclose()

        now = time.time()
        best_name: str | None = None
        best_score: float = -1.0

        for cap_name, raw in zip(cap_names, raws):
            if not raw:
                continue
            try:
                blob = json.loads(raw)
            except Exception:
                continue

            if blob.get("degraded"):
                continue  # Phase 5: skip degraded in scoring

            kw_hits = hit_counts[cap_name]
            total = blob.get("success_count", 0) + blob.get("failure_count", 0)
            success_rate = (blob.get("success_count", 0) / total) if total > 0 else 0.5
            last_used = blob.get("last_used", 0)
            age = now - last_used
            recency_bonus = 1.0 if age <= 86400 else (0.5 if age <= 604800 else 0.0)

            # Phase 2: weighted scoring with completeness history and latency
            history = blob.get("completeness_history", [])
            avg_completeness = (sum(history) / len(history)) if history else 1.0
            avg_latency_ms = blob.get("avg_latency_ms", 30000)
            latency_penalty = 0.5 if avg_latency_ms > 120000 else 0.0  # penalize >2min

            score = (kw_hits * 2) + (success_rate * 5) + (avg_completeness * 3) + recency_bonus - latency_penalty

            if score > best_score:
                best_score = score
                best_name = cap_name

        if best_score < MIN_SCORE:
            return None
        return best_name

    async def _load_capability(self, name: str) -> dict | None:
        r = aioredis.from_url(self._redis_url, decode_responses=True)
        try:
            raw = await r.get(f"capability:{name}")
            return json.loads(raw) if raw else None
        finally:
            await r.aclose()

    async def _record_hit(self, name: str, latency_ms: int, completeness: float) -> None:
        r = aioredis.from_url(self._redis_url, decode_responses=True)
        try:
            raw = await r.get(f"capability:{name}")
            if not raw:
                return
            blob = json.loads(raw)
            blob["success_count"] = blob.get("success_count", 0) + 1
            blob["last_used"] = time.time()

            # Phase 2: track latency EMA (α=0.3)
            prev_latency = blob.get("avg_latency_ms", latency_ms)
            blob["avg_latency_ms"] = int(0.7 * prev_latency + 0.3 * latency_ms)

            # Phase 5: track completeness history (last 10 runs)
            history = blob.get("completeness_history", [])
            history.append(round(completeness, 3))
            if len(history) > 10:
                history = history[-10:]
            blob["completeness_history"] = history

            # Phase 5: detect degradation — avg completeness < threshold over last 5 runs
            recent = history[-5:]
            if len(recent) >= 3:
                avg = sum(recent) / len(recent)
                if avg < MIN_COMPLETENESS:
                    blob["degraded"] = True
                    logger.warning("capability_engine.marked_degraded", cap=name,
                                   avg_completeness=round(avg, 2))

            await r.set(f"capability:{name}", json.dumps(blob), ex=86400 * 30)
        except Exception:
            pass
        finally:
            await r.aclose()

    async def _record_failure(self, name: str) -> None:
        r = aioredis.from_url(self._redis_url, decode_responses=True)
        try:
            raw = await r.get(f"capability:{name}")
            if raw:
                blob = json.loads(raw)
                blob["failure_count"] = blob.get("failure_count", 0) + 1
                blob["last_used"] = time.time()
                await r.set(f"capability:{name}", json.dumps(blob), ex=86400 * 30)
        except Exception:
            pass
        finally:
            await r.aclose()


# ─── Module-level helpers ───────────────────────────────────────────────────


def _tokenize(text: str) -> set[str]:
    words = re.findall(r"[a-záéíóúüñ]{4,}", text.lower())
    return {w for w in words if w not in _STOPWORDS}


def _pre_validate(steps: list[dict]) -> tuple[bool, str]:
    """
    Phase 3: Static validation before execution starts.
    Builds up the set of variables that will be available in ExecutionContext
    as each step completes, then verifies each step's required template args
    reference only variables that will exist by that point.
    Returns (ok, reason).
    """
    available_vars: set[str] = set()

    for i, step in enumerate(steps):
        skill = step.get("skill", "")
        args = step.get("args", {})

        for k, v in args.items():
            if k in _OPTIONAL_ARGS:
                continue
            v_str = str(v).strip()

            # Hard args must be non-empty even without templates
            if k in _HARD_ARGS and not v_str:
                return False, f"step {i} ({skill}): required arg '{k}' is empty"

            # Template args must reference variables available by this step
            if "{{" in v_str:
                for var in re.findall(r'\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}', v_str):
                    if var not in available_vars:
                        return False, (
                            f"step {i} ({skill}): '{k}' needs '{{{{{var}}}}}' "
                            f"not yet available (available: {sorted(available_vars)[:5]})"
                        )

        # Register outputs this step will make available
        available_vars.add(f"step_{i + 1}_output")
        if skill == "extract_fields":
            paths_str = str(args.get("paths", ""))
            for part in paths_str.split(","):
                part = part.strip()
                if ":" in part:
                    var_name = part.rsplit(":", 1)[1].strip()
                    if var_name:
                        available_vars.add(var_name)

    return True, "ok"


def _check_render_completeness(
    output: str, report_type: str, resolved_args: dict
) -> tuple[float, list[str]]:
    """
    Phase 4: Verify rendered output has all required data fields.
    Checks both:
    - resolved_args values are non-empty for required fields
    - output text has no unfilled {field} placeholders for required fields
    Returns (score 0-1, list of missing required field names).
    """
    required = _REQUIRED_RENDER_FIELDS.get(report_type, frozenset())
    if not required:
        return 1.0, []

    missing: list[str] = []

    # Check resolved arg values are non-empty
    for field in required:
        value = str(resolved_args.get(field, "")).strip()
        if not value:
            missing.append(field)

    # Also check rendered output for leftover {field} placeholders
    unfilled = set(_UNFILLED_RE.findall(output))
    for field in required:
        if field in unfilled and field not in missing:
            missing.append(field)

    score = max(0.0, (len(required) - len(missing)) / len(required))
    return score, missing


_RAW_SKILL_OUTPUT_RE = re.compile(
    r"Screenshot saved to|"
    r"/data/screenshots/|"
    r"⚠️ Verify the title|"
    r"\[skill:[a-z_]+\]|"
    r"Navigated to https?://|"
    r"Page: https?://",
    re.IGNORECASE,
)

def _validate_gmail_body(body: str) -> bool:
    """
    Phase 4: Email body must be substantial, contain no unfilled placeholders,
    and must not contain raw skill output artifacts.
    Returns False (block send) if any of these conditions are violated.
    """
    if not body or len(body.strip()) < 50:
        return False
    if _UNFILLED_RE.search(body):
        return False
    if _RAW_SKILL_OUTPUT_RE.search(body):
        return False
    return True
