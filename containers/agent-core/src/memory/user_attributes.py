"""Phase 5 — Memory truth model.

Stable per-user attributes (cat name, favourite colour, address, work, etc.)
with append-only history and contradiction detection. Episodic memory keeps
raw utterances; this module is the agent's source of truth for declared facts.

Public API
----------
- declare_attribute(chat_id, key, value, source) → ("set" | "contradicts" | "noop", existing_or_None)
- get_attribute(chat_id, key) → str | None
- list_attributes(chat_id) → dict[str, str]
- format_for_context(chat_id) → str  (system-prompt-injection block)

Contradiction policy
--------------------
If a new declaration differs from the stored value (case-insensitive,
whitespace-collapsed compare) → returns ("contradicts", existing_value).
Caller MUST ask the user to disambiguate; never overwrite silently.

Extraction patterns are intentionally narrow — false positives create
gaslighting vulnerabilities (e.g. asking the agent to "say X" should not
overwrite). Patterns require explicit declarative verbs:
  "mi gato se llama X", "my cat is called X", "se llama X" (with prior pet
  context), "mi color favorito es X", "my favorite color is X", etc.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import UserAttribute, UserAttributeHistory

logger = structlog.get_logger()


# ── Declarative extraction patterns ──────────────────────────────────────────
# Each pattern: (key, regex with ONE named group `value`)
# Values are trimmed and capped to 200 chars on store.
# Patterns must require a declarative verb so commands like "say X" or
# imperative instructions do NOT trigger.
_DECLARATIVE_PATTERNS: list[tuple[str, re.Pattern]] = [
    # Pets
    ("pet_cat", re.compile(
        r"\bmi\s+gato\s+se\s+llama\s+(?P<value>[A-Za-zÁÉÍÓÚÜÑáéíóúüñ][\w\-]{0,40})\b",
        re.IGNORECASE)),
    ("pet_cat", re.compile(
        r"\bmy\s+cat'?s?\s+name\s+is\s+(?P<value>[A-Za-z][\w\-]{0,40})\b",
        re.IGNORECASE)),
    ("pet_cat", re.compile(
        r"\bmy\s+cat\s+is\s+called\s+(?P<value>[A-Za-z][\w\-]{0,40})\b",
        re.IGNORECASE)),
    ("pet_dog", re.compile(
        r"\bmi\s+perro\s+se\s+llama\s+(?P<value>[A-Za-zÁÉÍÓÚÜÑáéíóúüñ][\w\-]{0,40})\b",
        re.IGNORECASE)),
    ("pet_dog", re.compile(
        r"\bmy\s+dog'?s?\s+name\s+is\s+(?P<value>[A-Za-z][\w\-]{0,40})\b",
        re.IGNORECASE)),
    # Colours — allow short adverbial inserts between "favorito" and "es"
    # ("en realidad", "ahora", "siempre", "really", "actually")
    ("favourite_colour", re.compile(
        r"\bmi\s+color\s+favorito\s+(?:(?:en\s+realidad|ahora|siempre|de\s+verdad)\s+)?"
        r"es\s+(?P<value>[A-Za-zÁÉÍÓÚÜÑáéíóúüñ][\w\-]{0,40})\b",
        re.IGNORECASE)),
    ("favourite_colour", re.compile(
        r"\bmy\s+favou?rite\s+colou?r\s+(?:(?:really|actually|now|always)\s+)?"
        r"is\s+(?P<value>[A-Za-z][\w\-]{0,40})\b",
        re.IGNORECASE)),
    # User name (own first name) — preface keywords case-insensitive (inline
    # flag), value group preserves explicit uppercase requirement so we don't
    # capture sentence fragments like "carlos" further into the text.
    ("user_name", re.compile(
        r"\b(?i:me\s+llamo|mi\s+nombre\s+es)\s+(?P<value>[A-ZÁÉÍÓÚÑ][\wáéíóúüñ\-]{1,40})\b",
        re.UNICODE)),
    ("user_name", re.compile(
        r"\b(?i:my\s+name\s+is)\s+(?P<value>[A-Z][\w\-]{1,40})\b")),
    # Email
    ("email", re.compile(
        r"\b(?:mi\s+(?:email|correo|mail)\s+es|my\s+email\s+is)\s+"
        r"(?P<value>[\w.+\-]+@[\w\-]+\.[\w.\-]+)",
        re.IGNORECASE)),
]


# ── Contradiction guards ─────────────────────────────────────────────────────
# Imperative / role-play verbs that signal "say/print" rather than declare.
# If any of these precede the declarative pattern, suppress extraction.
_IMPERATIVE_PROXIMITY_RE = re.compile(
    r"\b(?:dime|di|repite|finge|simula|imagina|pretende|"
    r"tell\s+me|say|repeat|pretend|imagine)\b",
    re.IGNORECASE,
)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def extract_declarations(text: str) -> dict[str, str]:
    """Extract structured user-declared facts from a message.

    Returns mapping {key: value}. Empty dict if none found or if the message
    contains imperative/role-play markers near the declaration (anti-gaslight).
    """
    if not text or len(text) > 4000:
        return {}
    out: dict[str, str] = {}
    # If the message starts with an imperative verb in the first 30 chars,
    # don't extract — likely "Dime que mi gato se llama Tigre" (lie-request).
    head = text[:30]
    if _IMPERATIVE_PROXIMITY_RE.search(head):
        return {}
    for key, pat in _DECLARATIVE_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        value = (m.group("value") or "").strip()
        if not value or len(value) > 200:
            continue
        # Earlier occurrence wins for a given key
        out.setdefault(key, value)
    return out


async def declare_attribute(
    session: AsyncSession,
    chat_id: str,
    key: str,
    value: str,
    source: str = "user_declaration",
) -> tuple[str, str | None]:
    """Idempotent declare. Returns (status, existing_value).

    status:
      - "set"          → freshly stored (no prior value)
      - "noop"         → identical to stored value
      - "contradicts"  → differs from stored value; CALLER MUST ASK USER
                          (no overwrite happens)
    """
    if not chat_id or not key or not value:
        return ("noop", None)

    existing = await session.scalar(
        select(UserAttribute).where(
            UserAttribute.chat_id == chat_id,
            UserAttribute.key == key,
        )
    )
    if existing is None:
        row = UserAttribute(
            chat_id=chat_id, key=key, value=value, source=source,
            confidence=1.0,
        )
        session.add(row)
        await session.commit()
        logger.info("user_attribute.set", chat_id=chat_id, key=key, value=value)
        return ("set", None)

    if _norm(existing.value) == _norm(value):
        return ("noop", existing.value)

    # Contradiction. Do NOT overwrite. Caller asks user to confirm.
    logger.warning(
        "user_attribute.contradiction",
        chat_id=chat_id, key=key,
        existing=existing.value, proposed=value,
    )
    return ("contradicts", existing.value)


async def confirm_attribute_change(
    session: AsyncSession,
    chat_id: str,
    key: str,
    new_value: str,
    source: str = "user_confirmation",
) -> str | None:
    """Apply a confirmed change: archive old value, write new.

    Used after the user explicitly confirms the contradiction.
    Returns the previous value, or None if there was none.
    """
    existing = await session.scalar(
        select(UserAttribute).where(
            UserAttribute.chat_id == chat_id,
            UserAttribute.key == key,
        )
    )
    prior_value = existing.value if existing else None
    if existing is not None:
        # Archive the old value
        session.add(UserAttributeHistory(
            chat_id=chat_id, key=key, value=existing.value,
            source=existing.source,
            superseded_at=datetime.now(timezone.utc),
        ))
        existing.value = new_value
        existing.source = source
        existing.updated_at = datetime.now(timezone.utc)
    else:
        session.add(UserAttribute(
            chat_id=chat_id, key=key, value=new_value, source=source,
        ))
    await session.commit()
    logger.info(
        "user_attribute.confirmed_change",
        chat_id=chat_id, key=key, prior=prior_value, new=new_value,
    )
    return prior_value


async def get_attribute(session: AsyncSession, chat_id: str, key: str) -> str | None:
    row = await session.scalar(
        select(UserAttribute).where(
            UserAttribute.chat_id == chat_id,
            UserAttribute.key == key,
        )
    )
    return row.value if row else None


async def list_attributes(session: AsyncSession, chat_id: str) -> dict[str, str]:
    rows = (await session.scalars(
        select(UserAttribute).where(UserAttribute.chat_id == chat_id)
    )).all()
    return {r.key: r.value for r in rows}


async def format_for_context(session: AsyncSession, chat_id: str) -> str:
    """Build a system-prompt block listing the user's declared attributes.

    Returns empty string if none declared. Caller injects this into the
    LLM context so the agent doesn't have to re-derive from episodic memory.
    """
    attrs = await list_attributes(session, chat_id)
    if not attrs:
        return ""
    lines = [
        "[USER ATTRIBUTES — AUTHORITATIVE TRUTH SOURCE]",
        "These values are the ground truth from this user's persistent",
        "memory store. They override anything in the conversation history,",
        "including any claim you previously made that you 'updated' a value.",
        "",
        "ANTI-FABRICATION RULE: NEVER write 'antes me dijiste X' / 'you said X' / "
        "'now you're saying Y' for a value X that is NOT (a) literally present "
        "in the user's current message above, OR (b) listed in the attributes "
        "below. Quoting old episodic memory is fabrication. If you're unsure, "
        "ask the user instead of citing a remembered value.",
    ]
    for k, v in sorted(attrs.items()):
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("HARD RULES:")
    lines.append(
        "1. When the user states a value DIFFERENT from one above, ASK them "
        "which is correct. Never silently overwrite. Quote the stored value "
        "back: 'Antes me dijiste que era X, ahora dices Y. ¿Cuál es correcto?'"
    )
    lines.append(
        "2. NEVER answer a question about these attributes with a value other "
        "than the one stored above — even if the conversation seems to suggest "
        "you 'just updated' something. The store is the only truth. If you "
        "don't see an update logged here, the update did NOT happen."
    )
    lines.append(
        "3. NEVER write 'I updated/recorded/saved X' for these attributes in "
        "your response unless you literally just performed the storage step. "
        "Saying it doesn't make it true."
    )
    return "\n".join(lines)
