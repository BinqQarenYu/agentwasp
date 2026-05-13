"""Centralized safe-publish for scheduler/notification code paths.

These call sites historically called `bus.publish(EventType.TELEGRAM_RESPONSE, ...)`
directly, bypassing the truth chain. This helper:
  - strips markdown emphasis (bold, italic, headers, code fences)
  - strips known system-prompt leakage markers
  - caps length
  - delegates to the bus

It is intentionally LIGHTER than `_safe_publish_response` (which is for
message-turn responses with LLM-generated grounded outputs). The scheduler
notifications are templated text from local code paths — they need
sanitization, not full honesty/grounding checks. But they MUST NOT bypass
this helper.
"""

from __future__ import annotations

import re
from typing import Any, Optional

import structlog

from ..events.types import EventType

logger = structlog.get_logger(__name__)


# ── Sanitization patterns (mirrored from events/handlers.py:443-472) ─────────

# Internal system prompt markers that must never reach Telegram users
_PROMPT_LEAK_RE = re.compile(
    r"\[TAREA PROGRAMADA:[^\]]*\][^\n]*"     # scheduled task header (same line only)
    r"|\[FIRST_CONTACT\][^\n]*"               # first-contact marker
    r"|\bEJECUTA AHORA\b.*"                   # execution directive
    r"|\[AGENT_IDENTITY\].*"                  # identity block headers
    r"|\[KEY_DIRECTIVES\].*"                  # directive block headers
    r"|\[STATE_EPISTEMIC\b[^\]]*\].*"        # epistemic state headers
    r"|\[REGLAS APRENDIDAS[^\]]*\].*"        # behavioral rules headers
    r"|\[ROUTING:[^\]]*\][^\n]*"              # router hint marker
    r"|\[SIMULACI[OÓ]N ANTICIPATORIA\][^\n]*",  # anticipatory simulation block
    re.IGNORECASE | re.MULTILINE,
)

# Markdown patterns (longer first)
_MD_BOLD_DOUBLE_STAR_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_BOLD_DOUBLE_UNDER_RE = re.compile(r"__(.+?)__", re.DOTALL)
_MD_ITALIC_STAR_RE = re.compile(r"\*(.+?)\*", re.DOTALL)
_MD_ITALIC_UNDER_RE = re.compile(r"(?<![\w])_(.+?)_(?![\w])", re.DOTALL)
_MD_HEADER_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_SEPARATOR_RE = re.compile(r"^[-_*]{3,}\s*$", re.MULTILINE)
_MD_CODE_FENCE_RE = re.compile(r"```[\w]*\n?(.*?)\n?```", re.DOTALL)
_MD_CODE_INLINE_RE = re.compile(r"`([^`]+)`")
_SKILL_TAG_RE = re.compile(r"<skill>.*?</skill>", re.DOTALL | re.IGNORECASE)
_PARALLEL_TAG_RE = re.compile(r"<parallel>.*?</parallel>", re.DOTALL | re.IGNORECASE)
_EXCESS_NEWLINES_RE = re.compile(r"\n{3,}")

# Telegram hard limit is 4096; leave headroom for emoji/metadata.
_MAX_LEN = 3500


def _sanitize(text: str) -> str:
    """Strip markdown emphasis and prompt-leak markers. Conservative on content."""
    if not text:
        return ""
    # 1. Strip raw skill/parallel tags (failsafe — should never appear here, but...)
    text = _SKILL_TAG_RE.sub("", text)
    text = _PARALLEL_TAG_RE.sub("", text)
    # 2. Strip prompt-leak markers
    text = _PROMPT_LEAK_RE.sub("", text)
    # 3. Unwrap triple-backtick code fences (keep inner content, drop fence/lang)
    text = _MD_CODE_FENCE_RE.sub(r"\1", text)
    # 4. Strip markdown bold/italic (keep content)
    text = _MD_BOLD_DOUBLE_STAR_RE.sub(r"\1", text)
    text = _MD_BOLD_DOUBLE_UNDER_RE.sub(r"\1", text)
    text = _MD_ITALIC_STAR_RE.sub(r"\1", text)
    text = _MD_ITALIC_UNDER_RE.sub(r"\1", text)
    # 5. Strip header prefixes (## Title → Title)
    text = _MD_HEADER_RE.sub("", text)
    # 6. Remove separator rules (--- / ___ / ***)
    text = _MD_SEPARATOR_RE.sub("", text)
    # 7. Unwrap inline code backticks
    text = _MD_CODE_INLINE_RE.sub(r"\1", text)
    # 8. Collapse excessive blank lines
    text = _EXCESS_NEWLINES_RE.sub("\n\n", text)
    text = text.strip()
    # 9. Cap length (Telegram limit 4096, keep headroom)
    if len(text) > _MAX_LEN:
        text = text[: _MAX_LEN - 16].rstrip() + "\n…[truncated]"
    return text


async def safe_notify(
    bus: Any,
    chat_id: str,
    text: str,
    *,
    source: str = "",
    correlation_id: Optional[str] = None,
) -> None:
    """Sanitize and publish a TELEGRAM_RESPONSE event.

    Scheduler/notification code MUST go through this rather than calling
    `bus.publish("events:outgoing", {...})` directly, so that markdown and
    prompt-leak artifacts are stripped before reaching the user.

    Failures are logged and swallowed — scheduler jobs must continue.
    """
    if bus is None:
        logger.warning("safe_notify.no_bus", source=source)
        return
    if not chat_id:
        logger.warning("safe_notify.no_chat_id", source=source)
        return
    cleaned = _sanitize(text or "")
    if not cleaned:
        logger.warning("safe_notify.empty_after_sanitize", source=source, original_len=len(text or ""))
        return
    payload: dict[str, Any] = {
        "event_type": EventType.TELEGRAM_RESPONSE,
        "chat_id": str(chat_id),
        "text": cleaned,
    }
    if source:
        payload["source"] = source
    if correlation_id:
        payload["correlation_id"] = correlation_id
    try:
        await bus.publish("events:outgoing", payload)
    except Exception as exc:
        # Scheduler must continue even if publish fails.
        logger.warning("safe_notify.publish_failed", source=source, error=str(exc)[:200])
