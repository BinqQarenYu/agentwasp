"""Living Self-Model — Agent Wasp's dynamic self-knowledge document.

A persistent, evolving JSON document stored in Redis that captures:
  - strengths: what the agent does well
  - known_failures: patterns that fail + solutions discovered
  - user_preferences: learned user behavior patterns
  - weekly_stats: execution metrics
  - improvement_queue: self-identified areas to improve
  - active_hours: when the user is typically active
  - skill_success_rates: per-skill success/failure counts

This is NOT the static system prompt — it's what the agent LEARNS about itself
through experience. Updated automatically after errors, successes, and dream cycles.

Injected into context as a compact block so the agent can make better decisions.
"""

import json
import os
import structlog
from datetime import datetime, timezone
from copy import deepcopy

import redis.asyncio as aioredis

logger = structlog.get_logger()

SELF_MODEL_KEY = "agent:self_model"
SELF_MODEL_VERSION_KEY = "agent:self_model:version"
_BACKUP_PATH = "/data/memory/self_model.json"

_DEFAULT_MODEL = {
    "version": 1,
    "updated_at": "",
    "strengths": [
        "web scraping with python_exec and browser",
        "crypto price retrieval via API",
        "task automation via shell",
        "web search and result analysis",
    ],
    "known_failures": [
        {
            "pattern": "JavaScript-heavy sites (React/Angular SPAs)",
            "solution": "use browser(action='navigate') with wait, or python_exec with playwright",
        },
        {
            "pattern": "direct AliExpress/Amazon scraping",
            "solution": "web_search(query='product site:aliexpress.com') — avoids CAPTCHA",
        },
    ],
    "user_preferences": {
        "language": "en",
        "response_style": "concise and direct",
        "preferred_crypto_source": "",
        "active_hours_start": 9,
        "active_hours_end": 23,
        "timezone": "America/Santiago",
    },
    "weekly_stats": {
        "week_start": "",
        "goals_completed": 0,
        "goals_failed": 0,
        "messages_processed": 0,
        "skill_calls": 0,
        "skills_used": {},
    },
    "improvement_queue": [
        "Improve data extraction from React-based pages",
        "Learn user usage patterns to anticipate needs",
    ],
    "skill_success_rates": {},
    "last_dream_at": "",
    "dream_count": 0,
    "total_messages_processed": 0,
    "known_context": [],  # brief facts agent knows about the current session
}


def _load_backup() -> dict | None:
    """Load self-model from file backup. Returns None if not available."""
    try:
        if os.path.isfile(_BACKUP_PATH):
            with open(_BACKUP_PATH, "r", encoding="utf-8") as f:
                model = json.load(f)
            merged = deepcopy(_DEFAULT_MODEL)
            _deep_merge(merged, model)
            logger.info("self_model.loaded_from_backup", path=_BACKUP_PATH)
            return merged
    except Exception:
        pass
    return None


def _save_backup(model: dict) -> None:
    """Write self-model to file backup (best-effort, never raises)."""
    try:
        os.makedirs(os.path.dirname(_BACKUP_PATH), exist_ok=True)
        with open(_BACKUP_PATH, "w", encoding="utf-8") as f:
            json.dump(model, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # Backup failure must never block the main flow


async def load(redis_url: str) -> dict:
    """Load self-model from Redis, falling back to file backup on cold Redis."""
    try:
        r = aioredis.from_url(redis_url, decode_responses=True)
        raw = await r.get(SELF_MODEL_KEY)
        await r.aclose()
        if raw:
            model = json.loads(raw)
            merged = deepcopy(_DEFAULT_MODEL)
            _deep_merge(merged, model)
            return merged
    except Exception:
        logger.exception("self_model.load_error")
    # Redis miss or error — try file backup before falling back to defaults
    backup = _load_backup()
    return backup if backup is not None else deepcopy(_DEFAULT_MODEL)


async def save(model: dict, redis_url: str) -> None:
    """Save self-model to Redis and keep file backup in sync."""
    try:
        model["updated_at"] = datetime.now(timezone.utc).isoformat()
        r = aioredis.from_url(redis_url, decode_responses=True)
        await r.set(SELF_MODEL_KEY, json.dumps(model))
        await r.incr(SELF_MODEL_VERSION_KEY)
        await r.aclose()
        # Write file backup (best-effort, non-blocking, survives Redis restarts)
        _save_backup(model)
    except Exception:
        logger.exception("self_model.save_error")
        # Even if Redis fails, attempt file backup
        _save_backup(model)


async def record_skill_result(skill_name: str, success: bool, redis_url: str) -> None:
    """Update skill success/failure counter in self-model."""
    try:
        model = await load(redis_url)
        rates = model.setdefault("skill_success_rates", {})
        entry = rates.setdefault(skill_name, {"success": 0, "failure": 0})
        if success:
            entry["success"] += 1
        else:
            entry["failure"] += 1

        # Also update weekly stats
        model["weekly_stats"]["skill_calls"] = model["weekly_stats"].get("skill_calls", 0) + 1
        skills_used = model["weekly_stats"].setdefault("skills_used", {})
        skills_used[skill_name] = skills_used.get(skill_name, 0) + 1

        await save(model, redis_url)
    except Exception:
        pass  # Never block skill execution on self-model update


async def record_message_processed(redis_url: str) -> None:
    """Increment message counter."""
    try:
        model = await load(redis_url)
        model["total_messages_processed"] = model.get("total_messages_processed", 0) + 1
        model["weekly_stats"]["messages_processed"] = model["weekly_stats"].get("messages_processed", 0) + 1
        await save(model, redis_url)
    except Exception:
        pass


async def record_failure_pattern(pattern: str, solution: str, redis_url: str) -> None:
    """Add or update a known failure pattern."""
    try:
        model = await load(redis_url)
        failures = model.setdefault("known_failures", [])

        # Check if pattern already exists
        for f in failures:
            if pattern.lower() in f["pattern"].lower():
                f["solution"] = solution  # Update solution
                await save(model, redis_url)
                return

        # Add new failure pattern
        failures.append({"pattern": pattern, "solution": solution})
        if len(failures) > 20:
            failures.pop(0)  # Keep last 20

        await save(model, redis_url)
    except Exception:
        pass


async def update_preference(key: str, value: str, redis_url: str) -> None:
    """Update a user preference in the self-model."""
    try:
        model = await load(redis_url)
        model["user_preferences"][key] = value
        await save(model, redis_url)
    except Exception:
        pass


def _is_duplicate_improvement(item: str, queue: list) -> bool:
    """Return True if item is semantically similar (>60% word overlap) to any existing entry."""
    words = set(item.lower().split())
    if not words:
        return False
    for existing in queue:
        ex_words = set(existing.lower().split())
        if not ex_words:
            continue
        overlap = len(words & ex_words) / max(len(words), len(ex_words))
        if overlap > 0.6:
            return True
    return False


async def add_to_improvement_queue(item: str, redis_url: str) -> None:
    """Add an item to the self-improvement queue."""
    try:
        model = await load(redis_url)
        queue = model.setdefault("improvement_queue", [])
        if not _is_duplicate_improvement(item, queue):
            queue.append(item)
            if len(queue) > 5:
                queue.pop(0)
            await save(model, redis_url)
    except Exception:
        pass


async def add_strength(strength: str, redis_url: str) -> None:
    """Add a discovered strength."""
    try:
        model = await load(redis_url)
        strengths = model.setdefault("strengths", [])
        if strength not in strengths:
            strengths.append(strength)
            if len(strengths) > 15:
                strengths.pop(0)
            await save(model, redis_url)
    except Exception:
        pass


async def update_from_dream(reflection: str, improvements: list[str], redis_url: str) -> None:
    """Update self-model with results from a dream cycle."""
    try:
        model = await load(redis_url)
        model["last_dream_at"] = datetime.now(timezone.utc).isoformat()
        model["dream_count"] = model.get("dream_count", 0) + 1

        # Reset weekly stats if new week
        week_start = model["weekly_stats"].get("week_start", "")
        now_week = datetime.now(timezone.utc).strftime("%Y-W%W")
        if week_start != now_week:
            model["weekly_stats"] = {
                "week_start": now_week,
                "goals_completed": 0,
                "goals_failed": 0,
                "messages_processed": 0,
                "skill_calls": 0,
                "skills_used": {},
            }

        # Add new improvements (with semantic dedup, cap at 5)
        queue = model.setdefault("improvement_queue", [])
        for imp in improvements:
            if imp and not _is_duplicate_improvement(imp, queue):
                queue.append(imp)
        model["improvement_queue"] = queue[-5:]

        await save(model, redis_url)
    except Exception:
        logger.exception("self_model.dream_update_error")


def format_for_context(model: dict) -> str:
    """Format self-model as a compact block for LLM context injection."""
    lines = ["[MY SELF-MODEL — what I know about myself:]"]

    # Top strengths
    strengths = model.get("strengths", [])
    if strengths:
        lines.append("  Strengths: " + ", ".join(strengths[:4]))

    # Known failure patterns (most recent). Defensive — Bug #12: malformed
    # failure entries (missing 'pattern' or 'solution' keys) used to crash
    # context.build_context with error='pattern'. Skip entries that lack the
    # required fields rather than aborting the whole block.
    failures = model.get("known_failures", [])
    valid_failures = [
        f for f in failures
        if isinstance(f, dict) and f.get("pattern") and f.get("solution")
    ]
    if valid_failures:
        lines.append("  Known failures and remedies:")
        for f in valid_failures[-3:]:
            lines.append(f"    • If I see '{f.get('pattern','')}' → {f.get('solution','')}")

    # User preferences
    prefs = model.get("user_preferences", {})
    pref_parts = []
    if prefs.get("preferred_crypto_source"):
        pref_parts.append(f"preferred crypto source: {prefs['preferred_crypto_source']}")
    if prefs.get("response_style"):
        pref_parts.append(f"style: {prefs['response_style']}")
    if pref_parts:
        lines.append("  User preferences: " + "; ".join(pref_parts))

    # Weekly stats
    stats = model.get("weekly_stats", {})
    total = model.get("total_messages_processed", 0)
    if total > 0:
        lines.append(f"  Total messages processed: {total}")

    # Skill success rates — top performers and worst
    rates = model.get("skill_success_rates", {})
    if rates:
        skill_rates = {
            k: v["success"] / max(1, v["success"] + v["failure"])
            for k, v in rates.items()
            if v["success"] + v["failure"] >= 3  # Only show skills with enough data
        }
        if skill_rates:
            best = max(skill_rates, key=skill_rates.get)
            worst = min(skill_rates, key=skill_rates.get)
            if skill_rates[best] > 0.9:
                lines.append(f"  Most reliable skill: {best} ({skill_rates[best]:.0%})")
            if skill_rates[worst] < 0.7 and worst != best:
                lines.append(f"  Most-failing skill: {worst} ({skill_rates[worst]:.0%}) — use with caution")

    # Next improvement target
    queue = model.get("improvement_queue", [])
    if queue:
        lines.append(f"  Next improvement: {queue[-1]}")

    return "\n".join(lines)


def _deep_merge(base: dict, update: dict) -> None:
    """Recursively merge update into base dict (in-place)."""
    for key, value in update.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
