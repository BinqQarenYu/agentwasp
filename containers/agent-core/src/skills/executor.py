import asyncio
import time
from collections import defaultdict
from uuid import uuid4

import structlog

from ..db.models import AuditLog
from ..db.session import async_session
from ..utils.redaction import redact
from .capability import CapabilityLevel, capability_registry
from .registry import SkillRegistry
from .simulation import risk_assessor
from .anticipatory import simulate as anticipatory_simulate, _needs_simulation
from .types import SkillCall, SkillResult

logger = structlog.get_logger()

MAX_TIMEOUT_SECONDS = 180.0


class SkillExecutor:
    """Validates, executes, and audits skill calls.

    Integrations:
    - CapabilityRegistry: classify each skill call by risk level
    - RiskAssessor: pre-execution analysis for RESTRICTED/PRIVILEGED skills
    - Per-skill execution counters for rate tracking (in-memory, per restart)
    """

    def __init__(self, registry: SkillRegistry, model_manager=None, redis_url: str = ""):
        self.registry = registry
        self.model_manager = model_manager
        self.redis_url = redis_url
        self._last_execution: dict[str, float] = {}
        # Per-skill hourly execution counter {skill_name: [timestamps]}
        self._exec_history: dict[str, list[float]] = defaultdict(list)

    async def execute(
        self,
        call: SkillCall,
        user_id: str = "",
        chat_id: str = "",
        execution_id: str = "",
    ) -> SkillResult:
        start = time.monotonic()
        skill_name = call.skill_name

        skill = self.registry.get(skill_name)
        if not skill:
            return SkillResult(
                skill_name=skill_name,
                success=False,
                output="",
                error=f"Skill '{skill_name}' not found or disabled.",
            )

        defn = skill.definition()

        # Cooldown check
        last = self._last_execution.get(skill_name, 0)
        elapsed = time.monotonic() - last
        if defn.cooldown_seconds > 0 and elapsed < defn.cooldown_seconds:
            remaining = defn.cooldown_seconds - elapsed
            return SkillResult(
                skill_name=skill_name,
                success=False,
                output="",
                error=f"Skill '{skill_name}' on cooldown. Try again in {remaining:.0f}s.",
            )

        # Validate required params
        for param in defn.params:
            if param.required and param.name not in call.arguments:
                if param.default is not None:
                    call.arguments[param.name] = param.default
                else:
                    return SkillResult(
                        skill_name=skill_name,
                        success=False,
                        output="",
                        error=f"Missing required parameter: {param.name}",
                    )

        # Capability-level checks
        cap_policy = capability_registry.get_policy(skill_name)

        # Rate-limit enforcement (PRIVILEGED only by default; others unlimited)
        if cap_policy.max_per_hour > 0:
            now_ts = time.monotonic()
            window = 3600.0
            history = [t for t in self._exec_history[skill_name] if now_ts - t < window]
            self._exec_history[skill_name] = history
            if len(history) >= cap_policy.max_per_hour:
                return SkillResult(
                    skill_name=skill_name,
                    success=False,
                    output="",
                    error=f"Skill '{skill_name}' exceeded {cap_policy.max_per_hour} executions/hour.",
                )

        # Pre-execution risk assessment for RESTRICTED/PRIVILEGED skills.
        # NOTE: This is WARN-ONLY — it logs detected risk but never blocks execution.
        # Execution blocking for dangerous operations is enforced by:
        #   1. file_ops.py path sandbox (_check_path)
        #   2. autonomy.py risk promotion for dangerous skill names
        #   3. self_improve.py CRIT-3 gate for install/rebuild
        if cap_policy.risk_assess:
            assessment = risk_assessor.assess(skill_name, call.arguments)
            if assessment.level.value != "low":
                logger.warning(
                    "skill_executor.risk_detected",
                    skill=skill_name,
                    risk=assessment.level.value,
                    reasons=assessment.reasons,
                    capability=cap_policy.level.value,
                    enforcement="warn_only",
                )

        # Anticipatory simulation for risky skill calls (non-blocking, appended to output)
        _simulation_note: str | None = None
        if self.model_manager and _needs_simulation(skill_name, call.arguments):
            try:
                _simulation_note = await anticipatory_simulate(
                    skill_name, call.arguments,
                    model_manager=self.model_manager,
                    redis_url=self.redis_url,
                )
            except Exception:
                pass  # Never block execution due to simulation failure

        # ── Cognitive Decision Layer (deterministic, no LLM call) ────────────
        # Consults: behavioral_rules.skill_poison, visual_memory, integrity
        # report, learning_examples.  Can BLOCK (rule poison) or WARN (notes
        # appended to output for LLM self-correction).
        _cognitive_note: str | None = None
        try:
            from ..agent.cognitive_decisions import evaluate as _cog_evaluate
            _cog = await _cog_evaluate(skill_name, dict(call.arguments), self.redis_url or "")
            if _cog.action == "block":
                logger.info(
                    "skill_executor.cognitive_block",
                    skill=skill_name,
                    source=_cog.source,
                    reason=_cog.reason[:120],
                )
                return SkillResult(
                    skill_name=skill_name,
                    success=False,
                    output="",
                    error=(
                        f"⛔ Cognitive layer blocked this call: {_cog.reason}. "
                        "Adjust your approach — do not retry the same action verbatim."
                    ),
                )
            if _cog.action == "warn" and _cog.note:
                _cognitive_note = _cog.note
                # Soft-steering backoff: only fires on repeated identical calls
                # after a WARN (first occurrence has 0s).  Bounded at 8s, never
                # blocks — just slows down loops so alternative paths surface.
                _backoff = float(_cog.extra.get("backoff_seconds", 0.0) or 0.0)
                _repeat = int(_cog.extra.get("repeat_count", 1) or 1)
                logger.info(
                    "skill_executor.cognitive_warn",
                    skill=skill_name,
                    source=_cog.source,
                    repeat=_repeat,
                    backoff_s=_backoff,
                )
                if _backoff > 0:
                    try:
                        await asyncio.sleep(min(_backoff, 8.0))
                    except Exception:
                        pass
        except Exception:
            pass  # Never block execution due to cognitive layer failure

        # Execute with timeout — pass context as kwargs
        call.arguments["chat_id"] = chat_id
        call.arguments["user_id"] = user_id
        if execution_id:
            call.arguments["_execution_id"] = execution_id
        timeout = min(defn.timeout_seconds, MAX_TIMEOUT_SECONDS)
        try:
            # Generated skills run in an isolated subprocess sandbox.
            # Builtin skills run in-process as before (they need full access).
            from .sandbox import is_sandboxable, execute_sandboxed
            if is_sandboxable(skill_name):
                result = await execute_sandboxed(skill_name, dict(call.arguments), timeout)
            else:
                result = await asyncio.wait_for(
                    skill.execute(**call.arguments),
                    timeout=timeout,
                )
            _ts = time.monotonic()
            self._last_execution[skill_name] = _ts
            self._exec_history[skill_name].append(_ts)
        except asyncio.TimeoutError:
            result = SkillResult(
                skill_name=skill_name,
                success=False,
                output="",
                error=f"Skill '{skill_name}' timed out after {timeout}s.",
            )
        except Exception as e:
            logger.exception("skill_executor.error", skill=skill_name)
            result = SkillResult(
                skill_name=skill_name,
                success=False,
                output="",
                error=f"Skill error: {str(e)}",
            )

        result.execution_ms = int((time.monotonic() - start) * 1000)

        # Append simulation prediction to output for LLM self-reflection.
        # [ADVISORY] — this annotation informs the LLM but never blocks execution.
        if _simulation_note and result.success:
            result.output = f"{result.output}\n\n[SIMULACIÓN ANTICIPATORIA — ADVISORY]: {_simulation_note}"

        # Append cognitive notes (visual memory / integrity / learning).
        # Always surfaced — both on success and failure — so the LLM can adjust
        # subsequent rounds based on what the cognitive systems actually know.
        if _cognitive_note:
            result.output = f"{result.output}\n\n{_cognitive_note}".strip()

        await self._audit(call, result, user_id, chat_id)

        # Non-blocking observability recording
        try:
            from ..observability.metrics import metrics as _metrics
            _metrics.record_skill(skill_name, result.execution_ms, result.success)
        except Exception:
            pass

        logger.info(
            "skill_executor.done",
            skill=skill_name,
            success=result.success,
            ms=result.execution_ms,
        )
        return result

    async def execute_batch(
        self,
        calls: list[SkillCall],
        user_id: str = "",
        chat_id: str = "",
        execution_id: str = "",
    ) -> list[SkillResult]:
        """Execute skill calls sequentially, but run parallel groups concurrently.

        Skills with the same parallel_group (from <parallel>...</parallel> blocks)
        are executed concurrently using asyncio.gather(). Sequential skills
        (parallel_group=None) run in order.
        """
        results = []
        i = 0
        while i < len(calls):
            call = calls[i]
            group = call.parallel_group

            if group is None:
                # Sequential: execute one at a time
                result = await self.execute(call, user_id, chat_id, execution_id)
                results.append(result)
                i += 1
            else:
                # Collect all consecutive calls sharing the same parallel_group
                group_calls = []
                while i < len(calls) and calls[i].parallel_group == group:
                    group_calls.append(calls[i])
                    i += 1
                # Execute all in the group concurrently
                logger.info(
                    "skill_executor.parallel_group",
                    group=group,
                    count=len(group_calls),
                    skills=[c.skill_name for c in group_calls],
                )
                group_results = await asyncio.gather(
                    *[self.execute(c, user_id, chat_id, execution_id) for c in group_calls]
                )
                results.extend(group_results)

        return results

    async def _audit(
        self, call: SkillCall, result: SkillResult, user_id: str, chat_id: str
    ):
        try:
            async with async_session() as session:
                cap_level = capability_registry.get_level(call.skill_name).value
                audit = AuditLog(
                    id=str(uuid4()),
                    event_type=f"skill.{cap_level}",
                    source="skill_executor",
                    action=f"skill.{call.skill_name}",
                    input_summary=redact(str(call.arguments))[:200],
                    output_summary=redact(((result.output or "") if result.success else (result.error or "")))[:200],
                    user_id=user_id,
                    chat_id=chat_id,
                    latency_ms=result.execution_ms,
                    error=result.error if not result.success else None,
                )
                session.add(audit)
                await session.commit()
        except Exception as _audit_err:
            logger.warning(
                "skill_executor.audit_error",
                skill=call.skill_name,
                error=str(_audit_err)[:120],
            )
