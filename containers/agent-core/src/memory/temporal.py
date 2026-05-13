"""Temporal World Model — tracks how the world changes over time.

Records timestamped observations: prices, events, user states, external facts.
Enables the agent to reason about trends, detect changes, and make temporal predictions.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import structlog
from sqlalchemy import select, and_

from ..db.session import async_session

logger = structlog.get_logger()

# Regex patterns to extract temporal observations from text
_PRICE_PATTERNS = [
    # "$65,000" / "65000 USD" / "BTC: $65k"
    re.compile(r'\b(BTC|ETH|bitcoin|ethereum|SOL|ADA|DOGE|XRP)\b.*?\$\s*([\d,]+(?:\.\d+)?[kKmM]?)', re.IGNORECASE),
    re.compile(r'\$\s*([\d,]+(?:\.\d+)?)\s*(?:USD|USDT|usd)?\s*(?:per|cada)?\s*(BTC|ETH|SOL|bitcoin|ethereum)', re.IGNORECASE),
]
_EVENT_PATTERNS = [
    re.compile(r'\b(lanzó|launched|anunció|announced|publicó|released|aprobó|approved|rechazó|rejected|compró|bought|vendió|sold)\b', re.IGNORECASE),
]
_STATE_PATTERNS = [
    re.compile(r'\b(sigo|todavía|aún|still|ya no|no longer|ahora|now|actualmente|currently)\b', re.IGNORECASE),
]

# NEW — numeric metric patterns with explicit entity. Avoids false positives by
# requiring "X is/at Y" or "Y of X" structure (not just any number in a sentence).
# Examples that match:
#   "CPU is at 87%"  → entity=CPU type=metric value=87%
#   "RAM at 72%"     → entity=RAM type=metric value=72%
#   "1500 users"     → entity=users type=count value=1500
#   "3 errors"       → entity=errors type=count value=3
_NUMERIC_METRIC_PATTERNS = [
    # "<entity> [verb] [~|aprox] <number>%/<unit>" — relaxed phrasing.
    # Verb is OPTIONAL ("CPU 87%" matches). "~" / "aprox" / "al" tolerated.
    # Entity must be in the allowlist; number must be directly attached to a
    # unit-token to count. No trailing \b because units like '%' are non-word.
    re.compile(
        r'\b(CPU|RAM|memoria|disk|disco|latency|latencia|uptime|carga|load|usuarios|users|errors?|errores?)\s*'
        r'(?:is\s+at|está\s+en|at|en|al|=|:|de|of|usage)?\s*'
        r'~?\s*(?:aprox\.?|approx\.?|about|around)?\s*'
        r'([\d.,]+\s*(?:%|ms\b|s\b|min\b|MB\b|GB\b|TB\b|req/s|rpm\b|qps\b))',
        re.IGNORECASE,
    ),
    # "~<number>% (of) <entity>" / "<number>% usage" — leading approximator
    # plus trailing entity-keyword. Unit required to bound the number side.
    re.compile(
        r'~?\s*([\d.,]+\s*(?:%|ms\b|s\b|min\b|MB\b|GB\b|TB\b))\s+(?:of\s+|de\s+)?'
        r'(CPU|RAM|memoria|disk|disco|latency|latencia|usage|uso|load|carga)\b',
        re.IGNORECASE,
    ),
    # "<number> <entity-noun>" — counts, not metrics. "1500 users", "3 errors today"
    re.compile(
        r'\b([\d,]{2,})\s+(usuarios|users|errores|errors|peticiones|requests|jobs|tareas|tasks)\b',
        re.IGNORECASE,
    ),
]

# NEW — temporal anchors: "hoy" / "today" / "now" / "currently" + entity keyword.
# Only fires when the sentence ALSO names a known observable entity, to avoid
# noise from "now I'll do X" type statements.
_TEMPORAL_ANCHOR_RE = re.compile(
    r'\b(hoy|today|ahora|now|currently|actualmente|esta\s+semana|this\s+week)\b',
    re.IGNORECASE,
)
_OBSERVABLE_ENTITY_RE = re.compile(
    r'\b(producci[oó]n|production|deploy|deployment|build|test|tests|servidor|server|'
    r'cluster|database|API|servicio|service|pipeline|sistema|system|app|aplicaci[oó]n)\b',
    re.IGNORECASE,
)

# NEW — system state changes: deploy/build/test outcomes
# "deploy finished", "build broke", "tests passed", "el sistema está caído"
_SYSTEM_STATE_PATTERNS = [
    re.compile(
        r'\b(deploy|deployment|build|tests?|pipeline|servidor|server|cluster|API|servicio|service)\s+'
        r'(?:is|está|esta|are|están)?\s*'
        r'(failed|broke|broken|caí[oa]|down|passed|pasó|paso|finished|terminó|termino|listo|ready|deployed|desplegad[oa]|crashed)\b',
        re.IGNORECASE,
    ),
]

# Patterns that indicate the text is NOT a real user state (contamination guards)
_CONTAMINATION_GUARDS = [
    re.compile(r'^\[TAREA', re.IGNORECASE),          # Scheduled task injections
    re.compile(r'EJECUTA AHORA', re.IGNORECASE),      # Task execution instructions
    re.compile(r'^\[REGLAS', re.IGNORECASE),           # Behavioral rules block
    re.compile(r'^\[OBSERV', re.IGNORECASE),           # Temporal observations block itself
    re.compile(r'^\[SOVEREIGN', re.IGNORECASE),        # Sovereign mode block
    re.compile(r'^\[ESTADO', re.IGNORECASE),           # Epistemic state block
    re.compile(r'^\[CONOCIMIENTO', re.IGNORECASE),     # KG block
    re.compile(r'^\[AUTO-MODELO', re.IGNORECASE),      # Self-model block
    re.compile(r'skill_name|arguments|task_graph', re.IGNORECASE),  # JSON plan text
    re.compile(r'^\s*[{}\[\]]'),                       # JSON/bracket-heavy lines
    re.compile(r'EJECUTA|INSTRUCCIÓN|INSTRUCCION', re.IGNORECASE),  # Instruction text
    re.compile(r'(acción proactiva|Acción Autónoma)', re.IGNORECASE),  # Autonomous action
]


def _is_valid_user_state(sentence: str) -> bool:
    """Return True only if the sentence is a genuine first-person user state observation.

    Rejects:
    - Task/scheduler injections
    - System prompt blocks
    - Long instructional text (> 200 chars is usually system text)
    - Sentences with no actual personal content
    """
    s = sentence.strip()
    if len(s) < 12 or len(s) > 200:
        return False
    for guard in _CONTAMINATION_GUARDS:
        if guard.search(s):
            return False
    # Must contain a first-person or personal pronoun or self-referential word
    if not re.search(r'\b(yo|me|mi|mis|sigo|tengo|estoy|trabajo|vivo|soy|I |my |I\'m)\b', s, re.IGNORECASE):
        return False
    return True


async def record_observation(
    entity: str,
    observation_type: str,
    value: str,
    source: str = "conversation",
    chat_id: str = "",
    confidence: float = 1.0,
    expires_hours: int | None = None,
    dedup_window_minutes: int | None = None,
) -> str:
    """Store a timestamped observation about an entity.

    Dedup: if the most recent observation for the same (entity, type, value)
    triple was recorded within the last `dedup_window_minutes`, skip the
    insert and return that row's id instead. Prevents spam when the same
    metric is mentioned multiple times in one conversation.

    Section 3 — dynamic window by type when caller does not specify:
      • metric  → 2 minutes (volatile, expected to change quickly)
      • state_change / temporal_mention / mention → 5 minutes (slower-moving)
      • default → 5 minutes
    """
    obs_id = str(uuid4())
    expires_at = None
    if expires_hours is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_hours)
    # Resolve dedup window based on observation_type when caller did not pass one
    if dedup_window_minutes is None:
        if observation_type == "metric":
            dedup_window_minutes = 2
        else:
            dedup_window_minutes = 5
    try:
        async with async_session() as session:
            from ..db.models import WorldTimeline
            # ── Dedup window check ─────────────────────────────────────
            if dedup_window_minutes > 0:
                cutoff = datetime.now(timezone.utc) - timedelta(minutes=dedup_window_minutes)
                dup_q = await session.execute(
                    select(WorldTimeline)
                    .where(and_(
                        WorldTimeline.entity == entity[:200],
                        WorldTimeline.observation_type == observation_type[:100],
                        WorldTimeline.value == value[:1000],
                        WorldTimeline.observed_at >= cutoff,
                    ))
                    .order_by(WorldTimeline.observed_at.desc())
                    .limit(1)
                )
                dup = dup_q.scalar_one_or_none()
                if dup:
                    logger.debug(
                        "temporal.dedup_skip",
                        entity=entity, type=observation_type, value=value[:50],
                        existing_id=dup.id,
                    )
                    return dup.id
            obs = WorldTimeline(
                id=obs_id,
                entity=entity[:200],
                observation_type=observation_type[:100],
                value=value[:1000],
                source=source[:100],
                chat_id=chat_id,
                confidence=confidence,
                expires_at=expires_at,
            )
            session.add(obs)
            await session.commit()
        logger.debug("temporal.recorded", entity=entity, type=observation_type, value=value[:50])
        # Section 5 — lightweight fire log: entity + type only, no value payload.
        logger.info("timeline.fire", entity=entity[:40], type=observation_type)
    except Exception as e:
        logger.debug("temporal.record_failed", error=str(e))
    return obs_id


async def get_entity_history(entity: str, days: int = 30, limit: int = 20) -> list[dict]:
    """Get timeline of observations for a specific entity."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        async with async_session() as session:
            from ..db.models import WorldTimeline
            result = await session.execute(
                select(WorldTimeline)
                .where(and_(
                    WorldTimeline.entity.ilike(f"%{entity}%"),
                    WorldTimeline.observed_at >= cutoff,
                ))
                .order_by(WorldTimeline.observed_at.desc())
                .limit(limit)
            )
            rows = result.scalars().all()
        return [
            {
                "id": r.id,
                "entity": r.entity,
                "type": r.observation_type,
                "value": r.value,
                "source": r.source,
                "observed_at": r.observed_at.isoformat(),
                "confidence": r.confidence,
            }
            for r in rows
        ]
    except Exception:
        return []


async def get_recent_observations(chat_id: str, hours: int = 24, limit: int = 15) -> list[dict]:
    """Get recent observations from all entities for a conversation."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    try:
        async with async_session() as session:
            from ..db.models import WorldTimeline
            result = await session.execute(
                select(WorldTimeline)
                .where(and_(
                    WorldTimeline.observed_at >= cutoff,
                    WorldTimeline.chat_id.in_([chat_id, ""]),
                ))
                .order_by(WorldTimeline.observed_at.desc())
                .limit(limit)
            )
            rows = result.scalars().all()
        return [
            {
                "entity": r.entity,
                "type": r.observation_type,
                "value": r.value,
                "observed_at": r.observed_at.isoformat(),
            }
            for r in rows
        ]
    except Exception:
        return []


async def detect_change(entity: str, current_value: str) -> dict | None:
    """Compare current value to last known value for an entity. Returns change info or None."""
    try:
        history = await get_entity_history(entity, days=7, limit=2)
        if len(history) >= 2:
            prev = history[1]  # Second most recent
            if prev["value"] != current_value:
                return {
                    "entity": entity,
                    "previous": prev["value"],
                    "current": current_value,
                    "previous_at": prev["observed_at"],
                }
    except Exception:
        pass
    return None


async def extract_from_text(text: str, source: str, chat_id: str) -> int:
    """Rule-based extraction of temporal observations from conversation text."""
    count = 0
    now = datetime.now(timezone.utc)

    # Extract crypto prices
    for pat_idx, pattern in enumerate(_PRICE_PATTERNS):
        for match in pattern.finditer(text):
            groups = match.groups()
            if len(groups) >= 2:
                # Pattern 0: (entity, value) — e.g. "BTC: $65,000"
                # Pattern 1: (value, entity) — e.g. "$65,000 per BTC"
                if pat_idx == 0:
                    entity = (groups[0] or "").upper()
                    value_str = groups[1] or ""
                else:
                    entity = (groups[1] or "").upper()
                    value_str = groups[0] or ""
                # Normalize entity name
                entity = entity.replace("BITCOIN", "BTC").replace("ETHEREUM", "ETH")
                if entity and value_str:
                    await record_observation(
                        entity=entity,
                        observation_type="price",
                        value=f"${value_str}",
                        source=source,
                        chat_id=chat_id,
                        expires_hours=48,
                    )
                    count += 1

    # Extract mentions of user state changes (only genuine first-person statements)
    if _STATE_PATTERNS[0].search(text):
        sentences = text.split(".")
        for sent in sentences:
            if _STATE_PATTERNS[0].search(sent) and _is_valid_user_state(sent):
                await record_observation(
                    entity="user_state",
                    observation_type="mention",
                    value=sent.strip()[:200],
                    source=source,
                    chat_id=chat_id,
                    expires_hours=72,
                )
                count += 1
                break  # one per message

    # NEW — Numeric metrics: "CPU at 87%", "RAM at 72%", "1500 users"
    for pattern in _NUMERIC_METRIC_PATTERNS:
        for m in pattern.finditer(text):
            groups = m.groups()
            if len(groups) >= 2 and groups[0] and groups[1]:
                # First pattern: (entity, value). Second: (value, entity).
                if groups[0][0].isdigit():
                    entity_raw, value_raw = groups[1], groups[0]
                else:
                    entity_raw, value_raw = groups[0], groups[1]
                entity = entity_raw.strip().upper()[:30]
                value = value_raw.strip()[:50]
                if entity and value:
                    await record_observation(
                        entity=entity,
                        observation_type="metric",
                        value=value,
                        source=source,
                        chat_id=chat_id,
                        expires_hours=24,
                    )
                    count += 1

    # NEW — Temporal anchor + observable entity: only fires when both are
    # present in the SAME sentence. "today the deploy finished" qualifies;
    # "now I'll do X" does not (no observable entity).
    for sent in text.split("."):
        sent_strip = sent.strip()
        if not sent_strip or len(sent_strip) > 200:
            continue
        if not _TEMPORAL_ANCHOR_RE.search(sent_strip):
            continue
        ent_match = _OBSERVABLE_ENTITY_RE.search(sent_strip)
        if not ent_match:
            continue
        # Avoid double-recording state mentions (already handled above)
        if _STATE_PATTERNS[0].search(sent_strip) and _is_valid_user_state(sent_strip):
            continue
        await record_observation(
            entity=ent_match.group(1).lower()[:30],
            observation_type="temporal_mention",
            value=sent_strip[:200],
            source=source,
            chat_id=chat_id,
            expires_hours=48,
        )
        count += 1
        break  # one per message

    # NEW — System state changes: "deploy finished", "build broke", "tests passed"
    for pattern in _SYSTEM_STATE_PATTERNS:
        for m in pattern.finditer(text):
            entity = m.group(1).lower()[:30]
            state_value = m.group(2).lower()[:30]
            await record_observation(
                entity=entity,
                observation_type="state_change",
                value=state_value,
                source=source,
                chat_id=chat_id,
                expires_hours=24,
            )
            count += 1

    return count


async def format_for_context(chat_id: str, hours: int = 48) -> str:
    """Format recent temporal observations for injection into system prompt."""
    try:
        observations = await get_recent_observations(chat_id=chat_id, hours=hours, limit=10)
        if not observations:
            return ""
        lines = ["[OBSERVACIONES TEMPORALES — lo que he registrado recientemente:]"]
        for obs in observations:
            dt = obs["observed_at"][:16].replace("T", " ")
            lines.append(f"• [{dt}] {obs['entity']}: {obs['value']} (fuente: {obs['type']})")
        return "\n".join(lines)
    except Exception:
        return ""
