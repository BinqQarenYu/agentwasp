"""BehavioralLearnerJob — analyzes user corrections and extracts behavioral rules.

Flow:
1. Every 120s, check Redis queue for pending corrections
2. For each correction, ask LLM: "what rule should the agent follow to avoid this?"
3. LLM returns structured JSON: rule_type, description, skill_poison, fewshot pair
4. Rule content is scanned for adversarial patterns before storage (HIGH-4 fix)
5. Rule saved to behavioral_rules DB table
6. build_context() injects active rules into every system prompt automatically
"""

from __future__ import annotations

import json
import re

import structlog

logger = structlog.get_logger()

# ── HIGH-4: Behavioral rule content filter ────────────────────────────────────
# Blocks rules whose description or skill_poison contains adversarial instructions
# that could be injected into the system prompt of future turns.
#
# Reject patterns target:
#   - instruction/prompt overrides ("ignore previous instructions")
#   - capability escalation ("always execute without restriction")
#   - security bypass ("skip domain lock", "bypass validation")
#   - mode overrides ("modo libre", "sin restricciones")
#   - privilege escalation ("always use sudo")
#
# Strategy: fail-closed for suspicious matches — log and discard rule.
_RULE_REJECT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"ignor[ea].*instrucciones?\s*(anteriores?|previas?|del\s+sistema?)", re.I),
     "instruction override (Spanish)"),
    (re.compile(r"ignore\s+(previous|prior|all|earlier)\s+instructions?", re.I),
     "instruction override (English)"),
    (re.compile(r"(?:skip|bypass|disable|deactivate|turn\s+off)\s+(?:the\s+)?"
                r"(?:domain|lock|validation|security|guard|check|control)", re.I),
     "security bypass directive"),
    (re.compile(r"(?:saltarte?|ignorar?|deshabilitar?)\s*"
                r"(?:dominio|domain|bloqueo|lock|validación|security|guard)", re.I),
     "security bypass directive (Spanish)"),
    (re.compile(r"(?:always|siempre)\s+execute\s+without\s+restriction", re.I),
     "unrestricted execution escalation"),
    (re.compile(r"(?:modo|mode)\s+libre", re.I),
     "mode override"),
    (re.compile(r"sin\s+restricciones", re.I),
     "restriction removal directive"),
    (re.compile(r"always\s+use\s+sudo", re.I),
     "privilege escalation"),
    (re.compile(r"you\s+must\s+(?:always\s+)?(?:execute|run|call|ignore|bypass)", re.I),
     "imperative capability escalation"),
    (re.compile(r"debes?\s+(?:siempre\s+)?(?:ejecutar|ignorar|saltar|evitar\s+(?:los?\s+)?(?:controles?|validaciones?))", re.I),
     "imperative capability escalation (Spanish)"),
    (re.compile(r"(?:override|overrule|circumvent)\s+(?:the\s+)?(?:domain|security|lock|guard|control)", re.I),
     "control override"),
    (re.compile(r"system\s+prompt\s+(?:override|injection|manipulation)", re.I),
     "prompt injection"),
    (re.compile(r"<\s*(?:system|SYSTEM|SYS|INST)\s*>", re.I),
     "embedded system tag"),
    (re.compile(r"\[SYSTEM\]|\[INST\]|\[\/?SYS\]", re.I),
     "embedded system tag"),
    # Exfiltration attempts — reveal system prompt / internal rules / secrets
    (re.compile(r"reveal\s+(?:the\s+)?(?:system\s+prompt|internal\s+rules?|secrets?|instructions?)", re.I),
     "exfiltration attempt"),
    (re.compile(r"(?:print|show|output|display|return|dump)\s+(?:your\s+)?(?:system\s+prompt|internal\s+rules?|configuration|api\s+keys?)", re.I),
     "exfiltration attempt"),
    (re.compile(r"ignore\s+(?:your|all|any)\s+(?:previous\s+)?instructions?", re.I),
     "instruction override (extended)"),
]


def _is_rule_safe(
    description: str,
    skill_poison: str | None,
    fewshot_user: str | None = None,
    fewshot_assistant: str | None = None,
) -> tuple[bool, str]:
    """Check rule content for adversarial patterns across all stored text fields.

    Returns (safe: bool, reason: str).
    safe=True  → content passes, proceed to storage.
    safe=False → content rejected; reason describes the matched pattern.

    All four text fields are scanned — fewshot_user and fewshot_assistant are
    injected directly into system prompts and must be validated here.
    """
    texts = [
        description or "",
        skill_poison or "",
        fewshot_user or "",
        fewshot_assistant or "",
    ]
    for pattern, reason in _RULE_REJECT_PATTERNS:
        for text in texts:
            if pattern.search(text):
                return False, reason
    return True, ""

_ANALYSIS_PROMPT = """You are analyzing an AI agent error to extract a behavioral rule.

A user corrected the agent after it made a mistake. Analyze the exchange and extract a rule.

User's original request:
{user_request}

Agent's bad response:
{agent_response}

User's correction:
{user_correction}

Extract a behavioral rule in JSON format. Choose exactly ONE rule_type:
- "refusal": agent said it couldn't do something it CAN do (e.g. "no puedo enviar correos")
- "hallucination": agent invented data that doesn't exist (emails, tasks, prices, names)
- "wrong_skill": agent used wrong approach or wrong skill
- "missing_context": agent didn't know about a connected service/account

Respond ONLY with valid JSON, no markdown, no explanation:
{{
  "rule_type": "refusal|hallucination|wrong_skill|missing_context",
  "description": "One clear actionable sentence for the agent (20-60 words). Start with a verb.",
  "skill_poison": "The exact 3-8 word phrase from the bad response that should be blocked (or null if none)",
  "fewshot_user": "The user's request rewritten as a clear, general example (or null)",
  "fewshot_assistant": "The correct agent response — if it involves a skill: <skill>skill_name(param=\\"value\\")</skill> (or null)"
}}"""


class BehavioralLearnerJob:
    """Processes queued user corrections and extracts behavioral rules via LLM."""

    def __init__(self, model_manager, redis_url: str, bus=None, notify_chat_id: str = ""):
        self._model_manager = model_manager
        self._redis_url = redis_url
        self._bus = bus
        self._notify_chat_id = notify_chat_id

    # Maximum retry attempts before a correction is permanently dropped.
    # Prevents infinite queue growth when LLM calls consistently fail.
    _MAX_RETRIES = 3

    async def __call__(self) -> str:
        from ..memory.behavioral import pop_pending_correction, save_rule, get_pending_count

        count = await get_pending_count()
        if count == 0:
            return "ok"

        processed = 0
        errors = 0
        dropped = 0

        # Process up to 5 corrections per run to avoid long blocking
        for _ in range(min(count, 5)):
            correction = await pop_pending_correction()
            if not correction:
                break

            # Dead-letter guard: track retry count inside the correction dict
            retry_count = int(correction.get("_retry_count", 0))

            try:
                rule = await self._analyze_correction(correction)
                if rule:
                    rule_id = await save_rule(
                        rule_type=rule["rule_type"],
                        description=rule["description"],
                        skill_poison=rule.get("skill_poison"),
                        fewshot_user=rule.get("fewshot_user"),
                        fewshot_assistant=rule.get("fewshot_assistant"),
                        source_exchange=correction,
                        confidence=0.9,
                    )
                    logger.info(
                        "behavioral_learner.rule_extracted",
                        rule_id=rule_id,
                        rule_type=rule["rule_type"],
                        description=rule["description"][:80],
                    )
                    processed += 1

                    # Notify via Telegram if configured
                    if self._bus and self._notify_chat_id:
                        await self._notify(rule)

            except Exception as e:
                retry_count += 1
                if retry_count < self._MAX_RETRIES:
                    # Re-queue with incremented retry counter
                    try:
                        import json as _json
                        import redis.asyncio as _aioredis
                        from ..memory.behavioral import _REDIS_PENDING_KEY
                        from ..config import settings as _cfg
                        correction["_retry_count"] = retry_count
                        _r = _aioredis.from_url(_cfg.redis_url, decode_responses=True)
                        try:
                            await _r.lpush(_REDIS_PENDING_KEY, _json.dumps(correction))
                        finally:
                            await _r.aclose()
                        logger.warning(
                            "behavioral_learner.analysis_failed_requeued",
                            error=str(e)[:120],
                            retry_count=retry_count,
                            max_retries=self._MAX_RETRIES,
                        )
                    except Exception as _qe:
                        logger.warning(
                            "behavioral_learner.requeue_failed",
                            error=str(_qe)[:120],
                        )
                else:
                    # Max retries reached — drop permanently and log
                    dropped += 1
                    logger.warning(
                        "behavioral_learner.correction_dropped",
                        reason="max_retries_exceeded",
                        retry_count=retry_count,
                        correction_preview=str(correction.get("user_correction", ""))[:80],
                    )
                errors += 1

        return f"behavioral_learner: processed={processed} errors={errors} dropped={dropped}"

    async def _analyze_correction(self, correction: dict) -> dict | None:
        """Ask LLM to analyze correction and return structured rule."""
        prompt = _ANALYSIS_PROMPT.format(
            user_request=correction.get("user_request", "")[:500],
            agent_response=correction.get("agent_response", "")[:500],
            user_correction=correction.get("user_correction", "")[:300],
        )

        try:
            from ..models.types import Message, ModelRequest
            messages = [
                Message(role="system", content="You are a behavioral analysis system. Respond ONLY with a single valid JSON object. No markdown, no explanation, no code blocks."),
                Message(role="user", content=prompt),
            ]
            response = await self._model_manager.generate(
                ModelRequest(messages=messages, max_tokens=400, temperature=0.1)
            )
            text = response.content.strip()

            logger.debug(
                "behavioral_learner.llm_raw_output",
                raw_len=len(text),
                raw_preview=text[:200],
            )

            # Strip markdown code block if present
            import re as _re
            md = _re.match(r"^```(?:json)?\s*\n?([\s\S]*?)\n?```$", text)
            if md:
                text = md.group(1).strip()

            # Try direct parse first; fallback: find first {...} block in text
            rule = None
            try:
                rule = json.loads(text)
            except json.JSONDecodeError:
                # Tolerant fallback: extract the first JSON object from surrounding text
                json_match = _re.search(r'\{[\s\S]*?\}', text)
                if json_match:
                    try:
                        rule = json.loads(json_match.group(0))
                        logger.info("behavioral_learner.json_extracted_from_noise",
                                    preview=json_match.group(0)[:80])
                    except json.JSONDecodeError:
                        pass
                if rule is None:
                    logger.warning(
                        "behavioral_learner.json_parse_failed",
                        raw_preview=text[:200],
                    )
                    return None

            # Validate required fields
            if not rule.get("rule_type") or not rule.get("description"):
                logger.warning("behavioral_learner.rule_missing_fields",
                               keys=list(rule.keys()))
                return None

            # Sanitize
            rule["rule_type"] = rule["rule_type"].strip().lower()
            if rule["rule_type"] not in ("refusal", "hallucination", "wrong_skill", "missing_context"):
                rule["rule_type"] = "refusal"

            # Normalize nulls
            for field in ("skill_poison", "fewshot_user", "fewshot_assistant"):
                if rule.get(field) in (None, "null", ""):
                    rule[field] = None

            # ── HIGH-4: Content safety filter ─────────────────────────────────
            # Reject rules that contain adversarial patterns before they reach
            # the DB and get injected into future system prompts.
            _safe, _reject_reason = _is_rule_safe(
                rule.get("description", ""),
                rule.get("skill_poison"),
                rule.get("fewshot_user"),
                rule.get("fewshot_assistant"),
            )
            if not _safe:
                logger.warning(
                    "behavioral_learner.rule_rejected_unsafe",
                    reject_reason=_reject_reason,
                    description_preview=rule.get("description", "")[:80],
                )
                return None
            # ── End HIGH-4 filter ──────────────────────────────────────────────

            return rule

        except Exception as e:
            logger.warning("behavioral_learner.analysis_exception", error=str(e)[:120])
            return None

    async def _notify(self, rule: dict) -> None:
        """Send a Telegram notification about the new behavioral rule."""
        from ..utils.safe_notify import safe_notify
        type_emoji = {
            "refusal": "🚫",
            "hallucination": "🌀",
            "wrong_skill": "🔧",
            "missing_context": "🔌",
        }.get(rule["rule_type"], "📌")
        text = (
            f"🧠 *Nueva regla aprendida* {type_emoji}\n"
            f"Tipo: `{rule['rule_type']}`\n"
            f"Regla: {rule['description']}"
        )
        try:
            await safe_notify(
                self._bus,
                self._notify_chat_id,
                text,
                source="behavioral_learner",
            )
        except Exception:
            pass
