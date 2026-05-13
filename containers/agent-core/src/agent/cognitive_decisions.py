"""Cognitive Decision Layer — closes the observe→decide→act loop.

Purely deterministic gate that runs immediately before each skill execution.
Consults four cognitive systems and produces a small adjustment to the planned
skill call.  No LLM call, no extra prompt tokens, no second reasoning loop.

Sources consulted (each cached / short-circuited so latency stays sub-ms in
the common case):

  1. Behavioral rules — `skill_poison` patterns from active rules.  When a
     pending skill call's signature matches a poison pattern, the call is
     BLOCKED and the reason is surfaced to the LLM as a SkillResult error.

  2. Visual memory — for browser/capture skills with a URL argument, looks
     up prior captures of the same URL.  If the most recent capture for that
     URL was marked invalid/blocked, the call is WARNED so the LLM can
     choose an alternative source instead of blindly retrying.

  3. Self-integrity report — reads the integrity monitor's findings from
     Redis (`agent:integrity_report`).  If the skill being invoked has a
     recent high error rate, a [INTEGRITY_WARN] note is appended to the
     output so the LLM reduces retries / prefers alternatives.

  4. Learning examples — when the skill is one that historically had
     negative outcomes for similar inputs, surface the lesson as a short
     [LEARNED] note.  Capped at top 1 example to prevent token explosion.

Decision shape:
  CognitiveDecision(action, reason, source, note)
    action: "allow" | "warn" | "block"
    reason: short explanation (logged + returned to LLM on block)
    source: which system produced the decision
    note:   text to append to skill output when action == "warn"
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger()


# ── Cache (process-local, refreshed by TTL) ──────────────────────────────────
_RULES_CACHE: dict = {"patterns": [], "fetched_at": 0.0}
_RULES_TTL = 60.0  # seconds — keep recent rules without hammering DB
_INTEGRITY_CACHE: dict = {"data": None, "fetched_at": 0.0}
_INTEGRITY_TTL = 60.0

# ── Soft-steering: track recent WARN signatures so repeated identical calls ──
# get escalated notes + a small back-off delay.  This is NOT blocking — it
# just slows down repeat-the-exact-same-mistake loops without forcing the LLM
# to pivot.  TTL is 5 min so old signatures don't accumulate.
import hashlib
_WARN_REPEAT_KEY_PREFIX = "cognitive:warn_repeat:"
_WARN_REPEAT_TTL = 300                # seconds
_BACKOFF_SCHEDULE = (0.0, 3.0, 5.0, 8.0)  # by repeat count: 1st→0s, 2nd→3s, 3rd→5s, 4th+→8s
_BACKOFF_MAX = 8.0


def _hash_signature(sig: str) -> str:
    """Short stable hash of the call signature for Redis keying."""
    return hashlib.sha256(sig.encode("utf-8")).hexdigest()[:16]


@dataclass
class CognitiveDecision:
    action: str = "allow"           # "allow" | "warn" | "block"
    reason: str = ""
    source: str = ""                # which subsystem produced the decision
    note: str = ""                  # text to surface to the LLM (warn only)
    extra: dict = field(default_factory=dict)


# ── Skill family classifiers (lightweight, no imports of skills package) ─────
_BROWSER_SKILLS = frozenset({
    "browser", "browser_smart_navigate", "browser_deep_scrape",
    "browser_screenshot_full_page", "scrape", "deep_scraper",
})
_NETWORK_SKILLS = frozenset({
    "browser", "browser_smart_navigate", "fetch_url", "http_request",
    "scrape", "web_search", "subscribe",
})


def _signature(skill_name: str, arguments: dict) -> str:
    """Build a short, lowercased blob of the call's signature for matching."""
    parts = [skill_name.lower()]
    for k, v in (arguments or {}).items():
        if k.startswith("_") or k in ("chat_id", "user_id"):
            continue
        try:
            sval = str(v)
        except Exception:
            continue
        parts.append(f"{k}={sval[:200]}")
    return " ".join(parts).lower()


# ── 1. Behavioral rules enforcement ──────────────────────────────────────────

async def _load_behavioral_poisons() -> list[tuple[re.Pattern, str]]:
    """Pull skill_poison patterns from active behavioral rules.

    Returns compiled (pattern, reason) tuples.  Cached for 60s.
    Each poison string can be either a substring (matched literally,
    case-insensitive) or a regex (auto-detected by leading 're:').
    """
    now = time.time()
    if _RULES_CACHE["patterns"] and (now - _RULES_CACHE["fetched_at"]) < _RULES_TTL:
        return _RULES_CACHE["patterns"]

    try:
        from ..memory.behavioral import get_active_rules
        rules = await get_active_rules(limit=40)
    except Exception:
        rules = []

    compiled: list[tuple[re.Pattern, str]] = []
    for r in rules:
        poison = (r.get("skill_poison") or "").strip()
        if not poison:
            continue
        try:
            if poison.startswith("re:"):
                compiled.append((re.compile(poison[3:], re.IGNORECASE),
                                 r.get("description", "behavioral rule")))
            else:
                # Literal substring → escaped regex
                compiled.append((re.compile(re.escape(poison), re.IGNORECASE),
                                 r.get("description", "behavioral rule")))
        except re.error:
            continue
    _RULES_CACHE["patterns"] = compiled
    _RULES_CACHE["fetched_at"] = now
    return compiled


async def _check_behavioral_rules(skill_name: str, arguments: dict) -> CognitiveDecision | None:
    """Return BLOCK decision if a behavioral rule's poison pattern matches."""
    poisons = await _load_behavioral_poisons()
    if not poisons:
        return None
    sig = _signature(skill_name, arguments)
    for pat, reason in poisons:
        if pat.search(sig):
            return CognitiveDecision(
                action="block",
                reason=f"behavioral rule blocks this action: {reason[:120]}",
                source="behavioral_rules",
            )
    return None


# ── 2. Visual memory consultation ────────────────────────────────────────────

def _normalize_url_for_match(url: str) -> str:
    """Strip query string, fragment, and trailing slash; lowercase the host
    so URL matching against visual_memory is robust to harmless variants."""
    if not url:
        return ""
    s = url.strip()
    # Drop fragment
    if "#" in s:
        s = s.split("#", 1)[0]
    # Drop query string
    if "?" in s:
        s = s.split("?", 1)[0]
    # Drop trailing slash (but keep "https://example.com" intact)
    if s.endswith("/") and s.count("/") > 2:
        s = s.rstrip("/")
    # Lowercase only the scheme://host portion (preserve case in path)
    try:
        if "://" in s:
            scheme, rest = s.split("://", 1)
            if "/" in rest:
                host, path = rest.split("/", 1)
                s = f"{scheme.lower()}://{host.lower()}/{path}"
            else:
                s = f"{scheme.lower()}://{rest.lower()}"
    except Exception:
        pass
    return s


async def _check_visual_memory(skill_name: str, arguments: dict) -> CognitiveDecision | None:
    """For browser/capture calls, warn if the same URL was previously invalid.

    URL matching is normalized (strip query/fragment/trailing-slash, lowercase
    host) so cosmetic differences don't bypass the check.  Falls back to ILIKE
    on the normalized form so partial matches still surface.
    """
    if skill_name not in _BROWSER_SKILLS:
        return None
    url = (arguments or {}).get("url") or ""
    if not url or not isinstance(url, str):
        return None
    norm = _normalize_url_for_match(url)
    if not norm:
        return None
    try:
        from ..db.session import async_session
        from ..db.models import VisualMemory
        from sqlalchemy import select, or_
        async with async_session() as session:
            stmt = (
                select(VisualMemory.description, VisualMemory.created_at, VisualMemory.url)
                .where(or_(
                    VisualMemory.url == url,
                    VisualMemory.url == norm,
                    VisualMemory.url.ilike(f"{norm}%"),
                ))
                .order_by(VisualMemory.created_at.desc())
                .limit(1)
            )
            row = (await session.execute(stmt)).first()
    except Exception:
        return None
    if not row:
        return None
    desc = (row[0] or "").lower()
    if any(k in desc for k in ("[capture_valid: false]", "blocked_source", "blocked", "invalid")):
        return CognitiveDecision(
            action="warn",
            reason="prior capture of this URL was marked invalid/blocked",
            source="visual_memory",
            note=(
                f"[VISUAL_MEMORY: a previous capture of {url[:80]} was invalid. "
                "Consider an alternative source — retrying may produce the same result.]"
            ),
        )
    return None


# ── 3. Self-integrity influence ──────────────────────────────────────────────

async def _load_integrity_report(redis_url: str) -> dict | None:
    """Fetch and cache the integrity monitor report (60s TTL)."""
    now = time.time()
    if _INTEGRITY_CACHE["data"] is not None and (now - _INTEGRITY_CACHE["fetched_at"]) < _INTEGRITY_TTL:
        return _INTEGRITY_CACHE["data"]
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(redis_url, decode_responses=True)
        try:
            raw = await r.get("agent:integrity_report")
        finally:
            await r.aclose()
    except Exception:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    _INTEGRITY_CACHE["data"] = data
    _INTEGRITY_CACHE["fetched_at"] = now
    return data


async def _check_integrity(skill_name: str, redis_url: str) -> CognitiveDecision | None:
    """Surface a warning when integrity flagged this skill recently.

    Two-tier read:
      1. Cached integrity report from Redis (full 24h scope, 6h refresh).
      2. If report is older than 1h, also do a quick 30-min audit_log probe
         so fast-moving failures aren't missed between integrity ticks.
    """
    if not redis_url:
        return None

    # Tier 1: cached report
    report = await _load_integrity_report(redis_url)
    expected_event = f"skill.{skill_name}".lower()
    if report:
        for finding in report.get("findings", []) or []:
            if finding.get("type") != "audit_error_spike":
                continue
            ev = (finding.get("event_type") or "").lower()
            if ev == expected_event or skill_name.lower() in ev:
                err_rate = float(finding.get("error_rate", 0) or 0)
                if err_rate >= 0.6:
                    return CognitiveDecision(
                        action="warn",
                        reason=f"integrity monitor: {ev} error_rate={err_rate:.0%}",
                        source="self_integrity",
                        note=(
                            f"[INTEGRITY_WARN: '{skill_name}' has high recent failure rate "
                            f"({err_rate:.0%}). Reduce retries; prefer an alternative skill if possible.]"
                        ),
                    )

    # Tier 2: stale-fallback audit_log probe (last 30 min, this skill only).
    # Runs only when the cached report is missing or older than 1 hour, so the
    # cognitive layer can react to fresh spikes between 6h integrity ticks.
    report_age = float("inf")
    if report:
        try:
            from datetime import datetime as _dt, timezone as _tz
            _gen = report.get("generated_at", "")
            if _gen:
                report_age = (_dt.now(_tz.utc) - _dt.fromisoformat(_gen.replace("Z", "+00:00"))).total_seconds()
        except Exception:
            pass
    if report_age >= 3600:  # report stale or missing
        try:
            from ..db.session import async_session
            from sqlalchemy import text
            async with async_session() as session:
                row = (await session.execute(text("""
                    SELECT COUNT(*) AS total,
                           COUNT(CASE WHEN error IS NOT NULL THEN 1 END) AS errors
                    FROM audit_log
                    WHERE action = :act
                    AND timestamp > NOW() - INTERVAL '30 minutes'
                """), {"act": expected_event})).first()
                if row and row[0] >= 3:
                    total, errors = int(row[0]), int(row[1])
                    rate = errors / max(total, 1)
                    if rate >= 0.6:
                        return CognitiveDecision(
                            action="warn",
                            reason=f"audit_log probe (30m): {expected_event} rate={rate:.0%}",
                            source="self_integrity",
                            note=(
                                f"[INTEGRITY_WARN: '{skill_name}' is currently failing "
                                f"({errors}/{total} in last 30m). Try a different approach.]"
                            ),
                        )
        except Exception:
            pass
    return None


# ── 4. Learning examples lookup ──────────────────────────────────────────────

async def _check_learning_examples(skill_name: str, arguments: dict) -> CognitiveDecision | None:
    """Surface a short lesson when a similar past call had a negative outcome."""
    if skill_name not in _NETWORK_SKILLS:
        # Limit to the families where examples are most informative
        return None
    sig_blob = _signature(skill_name, arguments)[:300]
    try:
        from ..db.session import async_session
        from ..db.models import LearningExample
        from sqlalchemy import select, or_
        async with async_session() as session:
            # Top-1 negative example whose recorded skill call mentions this skill
            stmt = (
                select(LearningExample.user_input, LearningExample.skill_calls)
                .where(
                    LearningExample.outcome == "negative",
                    LearningExample.skill_calls.ilike(f"%{skill_name}%"),
                )
                .order_by(LearningExample.use_count.desc())
                .limit(1)
            )
            row = (await session.execute(stmt)).first()
    except Exception:
        return None
    if not row:
        return None
    summary = (row[0] or "")[:120]
    return CognitiveDecision(
        action="warn",
        reason="similar past call had negative outcome",
        source="learning_examples",
        note=f"[LEARNED: similar past attempt failed → \"{summary}\". Consider an alternative.]",
    )


# ── Soft-steering: warn repeat tracking ──────────────────────────────────────

async def _bump_warn_counter(redis_url: str, sig_hash: str) -> int:
    """Increment the repeat counter for a warned signature, refresh TTL.

    Returns the new count (1 = first warn, 2 = second within window, …).
    Fail-open on Redis errors (returns 1 so behavior matches "first time").
    """
    if not redis_url or not sig_hash:
        return 1
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(redis_url, decode_responses=True)
        try:
            key = f"{_WARN_REPEAT_KEY_PREFIX}{sig_hash}"
            count = await r.incr(key)
            await r.expire(key, _WARN_REPEAT_TTL)
            return int(count)
        finally:
            await r.aclose()
    except Exception:
        return 1


def _escalation_note(repeat_count: int) -> str:
    """Return an additional steering line keyed to repeat count.

    First warn (count=1) → empty (regular note already shown).
    Repeat warns escalate the directive without changing the underlying notes.
    """
    if repeat_count <= 1:
        return ""
    if repeat_count == 2:
        return (
            "[STEERING: this is the 2nd attempt with the same parameters after a WARN. "
            "Strongly consider an alternative source / different parameters before retrying.]"
        )
    if repeat_count == 3:
        return (
            "[STEERING: 3rd identical retry after WARN. Pivot now — "
            "the failure pattern is clear; same input will not yield a different result.]"
        )
    return (
        f"[STEERING: {repeat_count}th identical retry after WARN. Stop repeating this exact call; "
        "use a fundamentally different approach.]"
    )


def _backoff_for_count(repeat_count: int) -> float:
    """Return the backoff (seconds) to apply before this retry executes."""
    if repeat_count <= 0:
        return 0.0
    if repeat_count - 1 < len(_BACKOFF_SCHEDULE):
        return _BACKOFF_SCHEDULE[repeat_count - 1]
    return _BACKOFF_MAX


# ── Public entrypoint ────────────────────────────────────────────────────────

async def evaluate(
    skill_name: str,
    arguments: dict,
    redis_url: str = "",
) -> CognitiveDecision:
    """Deterministic gate: combine the four cognitive checks.

    Order of precedence:
      1. behavioral_rules — can BLOCK
      2. visual_memory    — can WARN
      3. self_integrity   — can WARN
      4. learning_examples — can WARN

    Multiple WARN sources are merged into a single note. A BLOCK from rule
    enforcement short-circuits the rest.

    Soft-steering: when the SAME (skill, args) signature has been WARN'd
    before within 5 minutes, the note is escalated and a small backoff
    (0-8s) is suggested via `decision.extra["backoff_seconds"]`.  The
    SkillExecutor honors the backoff before invoking the skill.  This is
    NOT blocking — execution still proceeds.  It just slows repeat-the-
    exact-same-mistake loops naturally so alternative paths win out.
    """
    if not skill_name:
        return CognitiveDecision()

    notes: list[str] = []
    sources: list[str] = []

    try:
        d1 = await _check_behavioral_rules(skill_name, arguments)
        if d1 is not None and d1.action == "block":
            return d1
    except Exception:
        d1 = None

    for check_fn in (
        _check_visual_memory,
        lambda s, a: _check_integrity(s, redis_url),
        _check_learning_examples,
    ):
        try:
            d = await check_fn(skill_name, arguments)
        except Exception:
            d = None
        if d and d.action == "warn":
            notes.append(d.note)
            sources.append(d.source)

    if not notes:
        return CognitiveDecision()

    # Soft-steering: track repetition of THIS signature, escalate accordingly.
    sig_hash = _hash_signature(_signature(skill_name, arguments))
    repeat_count = await _bump_warn_counter(redis_url, sig_hash)
    backoff = _backoff_for_count(repeat_count)
    escalation = _escalation_note(repeat_count)

    final_note = "\n".join(notes)
    if escalation:
        final_note = f"{final_note}\n{escalation}"

    # Surface a one-line user-facing note so the operator sees that intelligence
    # actually changed behavior (closes the silent-intelligence UX gap).  Stored
    # in Redis with 30s TTL, picked up by handlers.py before sending the reply.
    chat_id = (arguments or {}).get("chat_id") if arguments else ""
    if chat_id and redis_url:
        if "behavioral_rules" in sources:
            user_note = "I declined the action because a learned rule applies."
        elif "visual_memory" in sources:
            user_note = "I avoided retrying that source — a previous capture was blocked."
        elif "self_integrity" in sources:
            user_note = "I kept retries low — that tool has been failing recently."
        elif "learning_examples" in sources:
            user_note = "I adjusted my approach based on a similar past failure."
        else:
            user_note = "I adjusted my approach based on past experience."
        try:
            import redis.asyncio as _aio
            r = _aio.from_url(redis_url, decode_responses=True)
            try:
                # Per-chat key with TTL — overwrites any prior note in same turn,
                # which is fine: the latest steering signal is the most relevant.
                await r.setex(f"chat:cognitive_note:{chat_id}", 30, user_note)
            finally:
                await r.aclose()
        except Exception:
            pass

    return CognitiveDecision(
        action="warn",
        reason=f"cognitive notes from {','.join(sources)} (repeat={repeat_count})",
        source=",".join(sources),
        note=final_note,
        extra={
            "backoff_seconds": backoff,
            "repeat_count": repeat_count,
            "signature_hash": sig_hash,
        },
    )
