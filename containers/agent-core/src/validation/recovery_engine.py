"""Auto-Recovery Engine — completes missing work instead of reporting failure.

PRINCIPLE: FIXING > EXPLAINING FAILURE

Failure modes handled:
  grounding_fail    → re-fetch real data via skill
  incomplete/email  → execute the email delivery skill
  incomplete/task   → execute the task scheduling skill
  missing_screenshot→ capture the missing browser screenshots
  drift             → one LLM correction round (no skill needed)

Flow per failure:
  1. Check RecoveryMemory for hints from past similar failures
  2. Build context-aware prompt (not hardcoded syntax)
  3. For skill-based failures: LLM generates skill call → execute → synthesize → re-validate
  4. For drift: LLM correction round only (no skill execution)
  5. Up to MAX_RETRIES=2 attempts; fallback only if all fail
  6. Record outcome in RecoveryMemory
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import structlog

from .response_validator import ResponseValidator, ValidationResult

logger = structlog.get_logger(__name__)

MAX_RETRIES = 2  # unchanged — no infinite loops


# ── Phase 1 — Context-aware recovery prompts (no hardcoded skill syntax) ─────
# LLM infers correct skill name and arguments from conversation context.

_PROMPT_GROUNDING = (
    "⚠️ AUTO-RECOVERY — DATA NOT GROUNDED\n"
    "Your previous response contained specific data (prices, statistics) "
    "that were not obtained from any skill execution. This is not acceptable.\n"
    "Execute the appropriate skill NOW to retrieve the real information the user requested. "
    "Determine which skill and what arguments to use from the conversation context. "
    "Output only the skill call — no explanatory text."
)

_PROMPT_EMAIL = (
    "⚠️ AUTO-RECOVERY — EMAIL NOT DELIVERED\n"
    "Your previous response indicated an email was sent, but the email skill was never executed. "
    "The report content and recipient details are available in this conversation.\n"
    "Execute the email delivery skill NOW using the data already collected. "
    "Output only the skill call — no explanatory text."
)

_PROMPT_TASK = (
    "⚠️ AUTO-RECOVERY — TASK NOT SCHEDULED\n"
    "Your previous response indicated a recurring task was created, but the scheduling skill "
    "was never executed.\n"
    "Execute the task scheduling skill NOW using the parameters from the user's original request. "
    "Output only the skill call — no explanatory text."
)

_PROMPT_SCREENSHOT = (
    "⚠️ AUTO-RECOVERY — SCREENSHOTS MISSING\n"
    "The user requested visual captures but no screenshots were taken during this execution.\n"
    "Execute the browser capture skill NOW for each URL or page that was requested. "
    "Use the URLs and session names from the conversation context. "
    "Output only the skill call(s) — no explanatory text."
)

_PROMPT_DRIFT = (
    "⚠️ AUTO-RECOVERY — TOPIC DRIFT DETECTED\n"
    "Your previous response did not address the user's actual request. "
    "Re-read the original request carefully and provide a response that directly and completely "
    "answers what was asked. Stay strictly on topic. Do not use skill tags."
)

_SYNTHESIS_PROMPT = (
    "Skill results:\n{results}\n\n"
    "Now provide the complete final response to the user in natural language. "
    "Confirm exactly what was completed. Do not use skill tags."
)

# ── Intent classification helpers ────────────────────────────────────────────

_EMAIL_RE = re.compile(
    r"\b(?:env[ií]a(?:me)?|send|manda(?:me)?|mail|email|correo|por\s+correo)\b",
    re.IGNORECASE,
)
_TASK_RE = re.compile(
    r"\b(?:programa(?:r)?|schedule|cada\s+\d|every\s+\d|recurrente|recurring|monitorea\s+cada|automatiza)\b",
    re.IGNORECASE,
)
_SCREENSHOT_RE = re.compile(
    r"\b(?:captura|screenshot|pantalla|imagen|photo|foto|ver\s+(?:la\s+)?p[aá]gina|muéstrame|toma\s+una)\b",
    re.IGNORECASE,
)
# ── Phase 2 — Screenshot completeness check (trace-based, standalone) ────────

def check_screenshot_completeness(
    response_text: str,
    user_input: str,
    executed_skills: set[str],
) -> ValidationResult:
    """Detect missing screenshots when user explicitly requested visual captures.

    Called from handlers.py AFTER the main ResponseValidator check.
    Uses execution trace only — does NOT rely on response string matching.
    Only triggers when:
      - user asked for screenshots/captures
      - no browser skill in the execution trace
    """
    if not _SCREENSHOT_RE.search(user_input):
        return ValidationResult(valid=True, reason="ok")

    _browser_ran = any("browser" in s for s in executed_skills)

    if not _browser_ran:
        return ValidationResult(
            valid=False,
            reason="missing_screenshot",
            should_retry=True,
            correction_hint="screenshots_missing",
            fallback_response=(
                "No pude tomar las capturas requeridas. "
                "¿Quieres que lo intente de nuevo?"
            ),
        )
    return ValidationResult(valid=True, reason="ok")


# ── Prompt selection ──────────────────────────────────────────────────────────

def _select_prompt(result: ValidationResult, user_input: str) -> str:
    """Return context-aware recovery prompt based on failure type and intent."""
    reason = result.reason
    if reason == "grounding_fail":
        return _PROMPT_GROUNDING
    if reason == "missing_screenshot":
        return _PROMPT_SCREENSHOT
    if reason == "drift":
        return _PROMPT_DRIFT
    if reason == "incomplete":
        if _EMAIL_RE.search(user_input):
            return _PROMPT_EMAIL
        if _TASK_RE.search(user_input):
            return _PROMPT_TASK
    # Generic: use whatever hint the validator computed
    return result.correction_hint or _PROMPT_GROUNDING


# ── Phase 4 — Recovery Memory ─────────────────────────────────────────────────

class RecoveryMemory:
    """Lightweight Redis-backed FIFO store of recovery outcomes.

    Structure per entry:
      {failure_type, intent_hash, fix_successful, timestamp}

    Behavior:
      - record(): append on each recovery attempt
      - get_hint(): return a short hint string if past success exists for same type+intent
      - Max 50 entries, FIFO (trim on write)
      - Fails silently — never blocks recovery flow
    """

    MAX_ENTRIES = 50
    REDIS_KEY = "recovery:patterns"
    TTL = 86400 * 7  # 7 days

    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self._redis = None

    async def _get_redis(self):
        if self._redis is None:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(self.redis_url, decode_responses=True)
        return self._redis

    @staticmethod
    def _intent_hash(user_input: str) -> str:
        """Rough intent fingerprint — first 60 chars, lowercased, alphanumeric only."""
        normalized = re.sub(r"[^a-z0-9\s]", "", user_input[:60].lower())
        return hashlib.md5(normalized.encode()).hexdigest()[:8]

    async def record(self, failure_type: str, user_input: str, fix_successful: bool) -> None:
        """Append validated-success entry only. Fails silently.

        Phase 1 safety rule: ONLY store patterns where recovery was validated
        (re-validation passed after skill execution). Never record failures —
        failed patterns add noise and cannot guide future hints.
        """
        if not fix_successful:
            return  # Never store failed recoveries
        try:
            r = await self._get_redis()
            entry = json.dumps({
                "failure_type": failure_type,
                "intent_hash": self._intent_hash(user_input),
                "validated": True,   # Phase 1: explicit validation flag
                "timestamp": int(time.time()),
            })
            pipe = r.pipeline()
            pipe.rpush(self.REDIS_KEY, entry)
            pipe.ltrim(self.REDIS_KEY, -self.MAX_ENTRIES, -1)
            pipe.expire(self.REDIS_KEY, self.TTL)
            await pipe.execute()
        except Exception as exc:
            logger.debug("recovery_memory.record_error", error=str(exc)[:60])

    async def get_hint(self, failure_type: str, user_input: str) -> str:
        """Return a hint string if past validated recoveries match type+intent. Empty if none."""
        try:
            r = await self._get_redis()
            raw_entries = await r.lrange(self.REDIS_KEY, -self.MAX_ENTRIES, -1)
            intent = self._intent_hash(user_input)
            for raw in reversed(raw_entries):
                try:
                    entry = json.loads(raw)
                    if (
                        entry.get("failure_type") == failure_type
                        and entry.get("validated") is True  # Phase 1: only validated entries
                        and entry.get("intent_hash") == intent
                    ):
                        return _HINT_MAP.get(failure_type, "")
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("recovery_memory.get_hint_error", error=str(exc)[:60])
        return ""


# Hint strings injected into recovery prompts when past success exists
_HINT_MAP = {
    "grounding_fail": (
        "[MEMORY HINT] Previous similar requests required fetching live data before responding. "
        "Prioritize the data-fetch skill call."
    ),
    "incomplete": (
        "[MEMORY HINT] Previous similar requests required completing all delivery steps "
        "(email, task creation) before responding. Ensure all steps run."
    ),
    "missing_screenshot": (
        "[MEMORY HINT] Previous similar requests required browser captures. "
        "Ensure all screenshots are taken before synthesizing the response."
    ),
    "drift": "",
}


# ── Phase 3 — Drift-specific recovery (LLM correction, no skill execution) ───

async def _recover_drift(
    validation_result: ValidationResult,
    response_text: str,
    user_input: str,
    messages: list,
    model_manager,
    cleanup_fn,
) -> tuple[str, bool]:
    """One LLM correction round for drift — no skill execution needed."""
    from ..models.types import Message, ModelRequest
    from ..skills.parser import strip_skill_calls

    cleanup = cleanup_fn or (lambda t: t)

    correction_messages = list(messages) + [
        Message(role="assistant", content=response_text),
        Message(role="user", content=_PROMPT_DRIFT),
    ]
    try:
        corrected = await model_manager.generate(
            ModelRequest(messages=correction_messages, image_path=None)
        )
        new_response = cleanup(strip_skill_calls(corrected.content))
        logger.info("recovery.drift_corrected", preview=new_response[:80])
        return new_response, True
    except Exception as exc:
        logger.error("recovery.drift_error", error=str(exc)[:80])
        return validation_result.fallback_response, False


# ── Main recovery entry point ─────────────────────────────────────────────────

async def attempt_recovery(
    validation_result: ValidationResult,
    response_text: str,
    user_input: str,
    messages: list,
    model_manager,
    skill_executor,
    user_id: str,
    chat_id: str,
    cleanup_fn=None,
    redis_url: str = "",
) -> tuple[str, bool]:
    """Attempt to fix a failed response by completing the missing work.

    Args:
        validation_result:  The ValidationResult that triggered recovery.
        response_text:      The response that failed validation.
        user_input:         The original user message this turn.
        messages:           Full conversation message list (not modified).
        model_manager:      ModelManager instance for LLM calls.
        skill_executor:     SkillExecutor instance for skill execution.
        user_id:            User ID string.
        chat_id:            Chat ID string.
        cleanup_fn:         Optional text cleanup callable.
        redis_url:          Optional Redis URL for RecoveryMemory.

    Returns:
        (response_text, recovered)
        recovered=True means the new response passed validation.
    """
    from ..models.types import Message, ModelRequest
    from ..skills.parser import parse_skill_calls, strip_skill_calls

    cleanup = cleanup_fn or (lambda t: t)
    memory = RecoveryMemory(redis_url) if redis_url else None

    # ── Drift: one correction round, no skill execution ───────────────────────
    if validation_result.reason == "drift":
        result_text, recovered = await _recover_drift(
            validation_result, response_text, user_input, messages, model_manager, cleanup_fn
        )
        if memory:
            await memory.record("drift", user_input, recovered)
        return result_text, recovered

    # ── Skill-based recovery loop (grounding_fail, incomplete, missing_screenshot) ─
    validator = ResponseValidator()
    current_validation = validation_result
    current_response = response_text
    recovered_skills: set[str] = set()

    for attempt in range(MAX_RETRIES):
        logger.warning(
            "recovery.attempt",
            attempt=attempt + 1,
            max=MAX_RETRIES,
            reason=current_validation.reason,
            user_input=user_input[:80],
        )

        # Phase 4: prepend memory hint if available
        base_prompt = _select_prompt(current_validation, user_input)
        if memory:
            hint = await memory.get_hint(current_validation.reason, user_input)
            recovery_prompt = f"{hint}\n\n{base_prompt}" if hint else base_prompt
        else:
            recovery_prompt = base_prompt

        # ── Step 1: Ask LLM to generate the missing skill call ────────────────
        recovery_messages = list(messages) + [
            Message(role="assistant", content=current_response),
            Message(role="user", content=recovery_prompt),
        ]
        try:
            skill_gen = await model_manager.generate(
                ModelRequest(messages=recovery_messages, image_path=None)
            )
            skill_gen_text = skill_gen.content
        except Exception as exc:
            logger.error("recovery.llm_error", attempt=attempt + 1, error=str(exc)[:80])
            continue

        # ── Step 2: Parse skill calls ─────────────────────────────────────────
        skill_calls = parse_skill_calls(skill_gen_text)
        if not skill_calls:
            logger.warning(
                "recovery.no_skills_generated",
                attempt=attempt + 1,
                llm_preview=skill_gen_text[:120],
            )
            continue

        logger.info(
            "recovery.executing",
            attempt=attempt + 1,
            skills=[c.skill_name for c in skill_calls],
        )

        # ── Step 3: Execute the skills ────────────────────────────────────────
        try:
            results = await skill_executor.execute_batch(
                skill_calls, user_id=user_id, chat_id=chat_id
            )
        except Exception as exc:
            logger.error("recovery.skill_error", attempt=attempt + 1, error=str(exc)[:80])
            continue

        for r in results:
            recovered_skills.add(r.skill_name)

        # ── Step 4: Synthesize final response ─────────────────────────────────
        results_lines = [
            f"[{sc.skill_name}] {(sr.output or 'OK')[:800]}" if sr.success
            else f"[{sc.skill_name}] ERROR: {sr.error or 'unknown'}"
            for sc, sr in zip(skill_calls, results)
        ]
        synthesis_messages = list(recovery_messages) + [
            Message(role="assistant", content=skill_gen_text),
            Message(role="user", content=_SYNTHESIS_PROMPT.format(results="\n".join(results_lines))),
        ]
        try:
            final = await model_manager.generate(
                ModelRequest(messages=synthesis_messages, image_path=None)
            )
            new_response = cleanup(strip_skill_calls(final.content))
        except Exception as exc:
            logger.error("recovery.synthesis_error", attempt=attempt + 1, error=str(exc)[:80])
            continue

        # ── Step 5: Re-validate ───────────────────────────────────────────────
        revalidation = validator.validate(
            response_text=new_response,
            user_input=user_input,
            executed_skills=recovered_skills,
            has_any_skill_data=True,
        )

        if revalidation.valid:
            logger.info(
                "recovery.success",
                attempt=attempt + 1,
                original_reason=validation_result.reason,
                skills_used=list(recovered_skills),
            )
            if memory:
                await memory.record(validation_result.reason, user_input, True)
            return new_response, True

        logger.warning(
            "recovery.still_invalid",
            attempt=attempt + 1,
            new_reason=revalidation.reason,
        )
        current_response = new_response
        current_validation = revalidation

    # ── All retries exhausted ─────────────────────────────────────────────────
    logger.error(
        "recovery.exhausted",
        max_retries=MAX_RETRIES,
        original_reason=validation_result.reason,
        user_input=user_input[:80],
    )
    # Phase 1: Do NOT record failed recoveries — noise, not signal.
    return validation_result.fallback_response, False
