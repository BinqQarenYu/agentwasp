"""Behavioral memory — stores and retrieves rules learned from user corrections.

Rules are persisted in PostgreSQL (behavioral_rules table) and cached in Redis.
Each rule was extracted by LLM analysis of a user correction exchange.
Rules are injected into every system prompt so the agent doesn't repeat mistakes.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Optional

import structlog

logger = structlog.get_logger()

_REDIS_CACHE_KEY = "behavioral:rules:cache"
_REDIS_CACHE_TTL = 300  # 5 minutes
_REDIS_PENDING_KEY = "behavioral:pending"  # List of pending corrections to analyze


# ── DB operations ──────────────────────────────────────────────────────────────

_NEGATION_WORDS = frozenset({
    "no", "not", "never", "don't", "dont", "avoid", "stop", "never",
    "no", "nunca", "jamás", "evita", "evitar", "no", "sin",
})

def _has_conflict(desc_a: str, desc_b: str) -> bool:
    """Return True if two rule descriptions likely contradict each other.

    Detects patterns like:
    - "always respond briefly" vs "respond in detail" (brevity vs verbosity)
    - "never use X" vs "always use X"
    - "do X" vs "do not do X"
    Strategy: extract key verb+noun pairs; flag if one has negation and the other doesn't
    for the same core words (>40% overlap after stripping negations).
    """
    words_a = set(desc_a.lower().split())
    words_b = set(desc_b.lower().split())
    # Core words: strip negations
    core_a = words_a - _NEGATION_WORDS
    core_b = words_b - _NEGATION_WORDS
    if not core_a or not core_b:
        return False
    overlap = len(core_a & core_b) / max(len(core_a), len(core_b))
    if overlap < 0.35:
        return False
    # Conflict if one has negation signals and the other doesn't
    neg_a = bool(words_a & _NEGATION_WORDS)
    neg_b = bool(words_b & _NEGATION_WORDS)
    return neg_a != neg_b


async def save_rule(
    rule_type: str,
    description: str,
    skill_poison: Optional[str] = None,
    fewshot_user: Optional[str] = None,
    fewshot_assistant: Optional[str] = None,
    source_exchange: Optional[dict] = None,
    confidence: float = 1.0,
) -> str:
    """Persist a new behavioral rule. Returns the new rule ID."""
    from ..db.session import async_session
    from ..db.models import BehavioralRule
    from sqlalchemy import select, update

    rule_id = str(uuid.uuid4())
    async with async_session() as session:
        # Dedup + conflict detection against existing active rules of same type
        existing = await session.execute(
            select(BehavioralRule).where(
                BehavioralRule.active == True,
                BehavioralRule.rule_type == rule_type,
            )
        )
        for row in existing.scalars():
            existing_words = set(row.description.lower().split())
            new_words = set(description.lower().split())
            if existing_words and new_words:
                overlap = len(existing_words & new_words) / max(len(existing_words), len(new_words))
                # Duplicate: skip
                if overlap > 0.6:
                    logger.info("behavioral.rule_duplicate_skipped", overlap=overlap)
                    return row.id
                # Conflict: flag the new rule
                if _has_conflict(row.description, description):
                    logger.warning(
                        "behavioral.rule_conflict_detected",
                        existing_id=row.id,
                        existing=row.description[:80],
                        new=description[:80],
                    )

        rule = BehavioralRule(
            id=rule_id,
            rule_type=rule_type,
            description=description,
            skill_poison=skill_poison,
            fewshot_user=fewshot_user,
            fewshot_assistant=fewshot_assistant,
            source_exchange=source_exchange or {},
            confidence=confidence,
            active=True,
            times_applied=0,
        )
        session.add(rule)
        await session.commit()
        logger.info("behavioral.rule_saved", rule_id=rule_id, rule_type=rule_type)

    # Invalidate cache
    try:
        import redis.asyncio as aioredis
        from ..config import settings
        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        try:
            await r.delete(_REDIS_CACHE_KEY)
        finally:
            await r.aclose()
    except Exception:
        pass

    return rule_id


async def get_active_rules(limit: int = 40) -> list[dict]:
    """Load active behavioral rules from DB (with Redis cache)."""
    # Try cache first
    try:
        import redis.asyncio as aioredis
        from ..config import settings
        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        try:
            cached = await r.get(_REDIS_CACHE_KEY)
            if cached:
                return json.loads(cached)
        finally:
            await r.aclose()
    except Exception:
        pass

    # Load from DB
    try:
        from ..db.session import async_session
        from ..db.models import BehavioralRule
        from sqlalchemy import select

        async with async_session() as session:
            result = await session.execute(
                select(BehavioralRule)
                .where(BehavioralRule.active == True)
                .order_by(BehavioralRule.times_applied.desc(), BehavioralRule.confidence.desc())
                .limit(limit)
            )
            rules = [
                {
                    "id": r.id,
                    "rule_type": r.rule_type,
                    "description": r.description,
                    "skill_poison": r.skill_poison,
                    "fewshot_user": r.fewshot_user,
                    "fewshot_assistant": r.fewshot_assistant,
                    "confidence": r.confidence,
                }
                for r in result.scalars()
            ]

        # Cache result
        try:
            import redis.asyncio as aioredis
            from ..config import settings
            r2 = aioredis.from_url(settings.redis_url, decode_responses=True)
            try:
                await r2.set(_REDIS_CACHE_KEY, json.dumps(rules), ex=_REDIS_CACHE_TTL)
            finally:
                await r2.aclose()
        except Exception:
            pass

        return rules
    except Exception as e:
        logger.warning("behavioral.load_rules_failed", error=str(e))
        return []


def format_for_context(rules: list[dict]) -> str:
    """Format behavioral rules for injection into the system prompt.

    Rules are ordered by times_applied DESC (most impactful first). Conflicting rules
    are suppressed: if a later rule conflicts with an already-accepted rule, it is skipped.
    Also schedules an async fire-and-forget increment of times_applied for each
    injected rule so effectiveness can be measured over time.
    """
    if not rules:
        return ""

    # Conflict pruning: iterate in priority order, skip rules that conflict with accepted ones
    accepted: list[dict] = []
    for candidate in rules:
        conflict = any(_has_conflict(candidate["description"], acc["description"]) for acc in accepted)
        if not conflict:
            accepted.append(candidate)
        else:
            logger.info(
                "behavioral.rule_conflict_suppressed_at_inject",
                suppressed=candidate["description"][:60],
            )
    rules = accepted

    lines = ["[RULES LEARNED FROM USER CORRECTIONS]"]
    lines.append("These rules were learned from past mistakes. You MUST follow them without exception:")
    for r in rules:
        prefix = {
            "refusal": "DO NOT REFUSE:",
            "hallucination": "DO NOT INVENT:",
            "wrong_skill": "USE THE CORRECT SKILL:",
            "missing_context": "CONTEXT:",
        }.get(r["rule_type"], "RULE:")
        lines.append(f"- [{prefix}] {r['description']}")

    # Increment times_applied asynchronously for all injected rules
    rule_ids = [r["id"] for r in rules if r.get("id")]
    if rule_ids:
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(_bulk_increment_applied(rule_ids))
        except Exception:
            pass

    return "\n".join(lines)


async def _bulk_increment_applied(rule_ids: list[str]) -> None:
    """Fire-and-forget: increment times_applied for each injected rule."""
    try:
        from ..db.session import async_session
        from ..db.models import BehavioralRule
        from sqlalchemy import update
        async with async_session() as session:
            await session.execute(
                update(BehavioralRule)
                .where(BehavioralRule.id.in_(rule_ids))
                .values(times_applied=BehavioralRule.times_applied + 1)
            )
            await session.commit()
        # Invalidate cache so next load reflects updated counts
        try:
            import redis.asyncio as aioredis
            from ..config import settings
            r = aioredis.from_url(settings.redis_url, decode_responses=True)
            try:
                await r.delete(_REDIS_CACHE_KEY)
            finally:
                await r.aclose()
        except Exception:
            pass
    except Exception:
        pass


def extract_fewshots(rules: list[dict]) -> list[tuple[str, str]]:
    """Extract valid few-shot pairs from behavioral rules."""
    pairs = []
    for r in rules:
        if r.get("fewshot_user") and r.get("fewshot_assistant"):
            pairs.append((r["fewshot_user"], r["fewshot_assistant"]))
    return pairs


def extract_poison_patterns(rules: list[dict]) -> list[str]:
    """Extract skill poison patterns from behavioral rules."""
    return [r["skill_poison"] for r in rules if r.get("skill_poison")]


# ── Correction queue ───────────────────────────────────────────────────────────

async def queue_correction(
    user_request: str,
    agent_response: str,
    user_correction: str,
    chat_id: str = "",
) -> None:
    """Queue a correction exchange for LLM analysis by BehavioralLearnerJob."""
    try:
        import redis.asyncio as aioredis
        from ..config import settings
        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        try:
            payload = json.dumps({
                "user_request": user_request[:1000],
                "agent_response": agent_response[:1000],
                "user_correction": user_correction[:500],
                "chat_id": chat_id,
                "queued_at": datetime.now(timezone.utc).isoformat(),
            })
            before = await r.llen(_REDIS_PENDING_KEY)
            await r.lpush(_REDIS_PENDING_KEY, payload)
            await r.ltrim(_REDIS_PENDING_KEY, 0, 49)  # Cap: max 50 items, drop oldest
            after = await r.llen(_REDIS_PENDING_KEY)
            dropped = max(0, before + 1 - after)
            if dropped:
                logger.warning("behavioral.queue_cap_trimmed", dropped=dropped, queue_depth=after)
            else:
                logger.info("behavioral.correction_queued", chat_id=chat_id, queue_depth=after)
        finally:
            await r.aclose()
    except Exception as e:
        logger.warning("behavioral.queue_correction_failed", error=str(e))


async def pop_pending_correction() -> Optional[dict]:
    """Pop one pending correction from the queue. Returns None if empty."""
    try:
        import redis.asyncio as aioredis
        from ..config import settings
        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        try:
            raw = await r.rpop(_REDIS_PENDING_KEY)
            return json.loads(raw) if raw else None
        finally:
            await r.aclose()
    except Exception:
        return None


async def get_pending_count() -> int:
    """Return number of corrections waiting to be analyzed."""
    try:
        import redis.asyncio as aioredis
        from ..config import settings
        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        try:
            return await r.llen(_REDIS_PENDING_KEY)
        finally:
            await r.aclose()
    except Exception:
        return 0


async def disable_rule(rule_id: str) -> bool:
    """Disable a behavioral rule by ID."""
    try:
        from ..db.session import async_session
        from ..db.models import BehavioralRule
        async with async_session() as session:
            rule = await session.get(BehavioralRule, rule_id)
            if rule:
                rule.active = False
                await session.commit()
                # Invalidate cache
                import redis.asyncio as aioredis
                from ..config import settings
                r = aioredis.from_url(settings.redis_url, decode_responses=True)
                try:
                    await r.delete(_REDIS_CACHE_KEY)
                finally:
                    await r.aclose()
                return True
        return False
    except Exception:
        return False
