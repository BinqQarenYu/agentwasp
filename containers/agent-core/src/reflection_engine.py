"""Self-Reflection Engine — autonomous learning from completed/failed task execution.

Two reflection layers:

Layer 1 — Goal Reflections (existing, Redis-based)
  Triggers on goal_completed / goal_failed.
  Generated via LLM.  Stored in Redis with 7-day TTL.

Layer 2 — Execution Reflections (new, DB-based)
  Triggers after EVERY message turn.
  Generated via fast heuristics (no LLM).
  Stored in PostgreSQL execution_reflections table (max 1000 rows).
  Pattern detection marks recurring patterns when pattern_key appears ≥3 times.

Both layers surface via build_context() as [REFLEXIONES APRENDIDAS] block.

Execution reflection structure:
  {
    "timestamp":        ISO-8601,
    "intent":           str,           # first 150 chars of user input
    "success":          bool,
    "efficiency_score": float 0–1,
    "issues":           list[str],
    "insight":          str,
    "suggestion":       str,
    "pattern_key":      str,           # canonicalized skill-set + outcome
    "recurring_pattern": bool,
  }
"""
from __future__ import annotations

import json
import structlog
from datetime import datetime, timezone, timedelta
from typing import Any
from uuid import uuid4

import redis.asyncio as aioredis

from .models.manager import ModelManager
from .models.types import Message, ModelRequest

logger = structlog.get_logger()

_KEYWORD_OVERLAP_STOP = frozenset({
    "el", "la", "los", "las", "un", "una", "de", "en", "a",
    "the", "an", "of", "in", "to", "for", "is", "it",
    "run", "execute", "process", "handle", "parse",
    "do", "make", "create"
})

# ── Execution Reflection constants ────────────────────────────────────────────
EXEC_REFLECTION_MAX_ROWS = 1_000   # hard cap — pruner deletes oldest above this
EXEC_PATTERN_THRESHOLD   = 3      # occurrences before pattern is flagged "recurring"

MAX_REFLECTIONS_PER_GOAL = 3
REFLECTION_TTL_SECONDS = 7 * 86400   # 7 days
RECENT_GOAL_LIST_SIZE = 10
REFLECTION_KEY_PREFIX = "reflection:"
REFLECTION_RECENT_KEY = "reflection:recent_goals"

_REFLECTION_SYSTEM_PROMPT = (
    "You are a concise self-reflection engine for an autonomous AI agent called WASP. "
    "Your job is to analyze task execution results and generate a single actionable learning insight. "
    "Be specific, brief (1-3 sentences), and focus on what can be improved next time. "
    "Respond only with the insight text — no labels, no JSON, no markdown."
)


class ReflectionEngine:
    """Analyzes completed/failed goals and generates learning insights."""

    def __init__(
        self,
        model_manager: ModelManager,
        redis_url: str,
    ):
        self.model_manager = model_manager
        self.redis_url = redis_url

    # ──────────────────────────────────────────────────────────────────────
    # Main Entry Point — called from GoalOrchestrator after state transition
    # ──────────────────────────────────────────────────────────────────────

    async def reflect_on_goal(
        self,
        goal_id: str,
        objective: str,
        outcome: str,            # "success" | "failure"
        error: str = "",
        task_summaries: list[str] | None = None,
        task_id: str | None = None,
    ) -> bool:
        """Generate and store a reflection for a completed/failed goal.

        Returns True if reflection was stored, False if skipped.
        """
        if not goal_id:
            return False

        logger.info(
            "reflection_triggered",
            goal_id=goal_id,
            outcome=outcome,
            task_id=task_id,
        )

        # Don't exceed max reflections per goal
        existing = await self._load_reflections(goal_id)
        if len(existing) >= MAX_REFLECTIONS_PER_GOAL:
            logger.debug("reflection_engine.max_reached", goal_id=goal_id)
            return False

        # Generate reflection via LLM
        reflection_text = await self._generate_reflection(
            objective=objective,
            outcome=outcome,
            error=error,
            task_summaries=task_summaries or [],
        )
        if not reflection_text:
            return False

        # Compute importance: failures are more important to learn from
        importance = 0.8 if outcome == "failure" else 0.5
        if error:
            importance = min(1.0, importance + 0.2)

        reflection_entry = {
            "goal_id": goal_id,
            "task_id": task_id,
            "reflection": reflection_text,
            "outcome": outcome,
            "importance": importance,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        await self._save_reflection(goal_id, reflection_entry)
        await self._register_recent_goal(goal_id)

        logger.info(
            "reflection_saved",
            goal_id=goal_id,
            task_id=task_id,
            reflection_summary=reflection_text[:120],
            outcome=outcome,
            importance=importance,
        )
        return True

    # ──────────────────────────────────────────────────────────────────────
    # Retrieval — for build_context()
    # ──────────────────────────────────────────────────────────────────────

    async def get_reflections_for_goal(self, goal_id: str) -> list[dict]:
        """Return stored reflections for a specific goal (ordered by importance)."""
        reflections = await self._load_reflections(goal_id)
        return sorted(reflections, key=lambda r: r.get("importance", 0), reverse=True)

    async def get_recent_reflections(self, limit: int = 2) -> list[dict]:
        """Return the most important recent reflections across all goals."""
        if not self.redis_url:
            return []
        try:
            r = aioredis.from_url(self.redis_url, decode_responses=True)
            try:
                raw = await r.get(REFLECTION_RECENT_KEY)
                if not raw:
                    return []
                recent_goal_ids: list[str] = json.loads(raw)
            finally:
                await r.aclose()

            all_reflections: list[dict] = []
            for gid in recent_goal_ids[:RECENT_GOAL_LIST_SIZE]:
                entries = await self._load_reflections(gid)
                all_reflections.extend(entries)

            # Sort by importance then recency, cap at limit
            all_reflections.sort(
                key=lambda r: (r.get("importance", 0), r.get("timestamp", "")),
                reverse=True,
            )
            return all_reflections[:limit]
        except Exception as exc:
            logger.debug("reflection_engine.get_recent_failed", error=str(exc)[:120])
            return []

    # ──────────────────────────────────────────────────────────────────────
    # LLM Generation
    # ──────────────────────────────────────────────────────────────────────

    async def _generate_reflection(
        self,
        objective: str,
        outcome: str,
        error: str = "",
        task_summaries: list[str] | None = None,
    ) -> str:
        """Ask LLM to generate a concise learning insight."""
        if not self.model_manager:
            return ""

        outcome_label = "success" if outcome == "success" else "failure"
        tasks_section = ""
        if task_summaries:
            tasks_section = "\nExecuted tasks:\n" + "\n".join(
                f"  {i+1}. {s}" for i, s in enumerate(task_summaries[:5])
            )

        error_section = f"\nPrimary error: {error[:300]}" if error else ""

        prompt = (
            f"Goal objective: {objective[:300]}\n"
            f"Result: {outcome_label}{error_section}{tasks_section}\n\n"
            "Generate a concise (1-3 sentence) learning insight about "
            "what worked, what failed, and how to improve next time."
        )

        try:
            request = ModelRequest(
                messages=[
                    Message(role="system", content=_REFLECTION_SYSTEM_PROMPT),
                    Message(role="user", content=prompt),
                ],
                max_tokens=200,
            )
            response = await self.model_manager.generate(request)
            text = (response.content or "").strip()
            # Strip any accidental markdown
            text = text.replace("**", "").replace("##", "").replace("# ", "")
            return text[:500] if text else ""
        except Exception as exc:
            logger.debug("reflection_engine.llm_failed", error=str(exc)[:120])
            return ""

    # ──────────────────────────────────────────────────────────────────────
    # Redis Storage
    # ──────────────────────────────────────────────────────────────────────

    async def _load_reflections(self, goal_id: str) -> list[dict]:
        if not self.redis_url:
            return []
        try:
            r = aioredis.from_url(self.redis_url, decode_responses=True)
            try:
                key = f"{REFLECTION_KEY_PREFIX}{goal_id}"
                raw = await r.get(key)
                return json.loads(raw) if raw else []
            finally:
                await r.aclose()
        except Exception:
            return []

    async def _save_reflection(self, goal_id: str, entry: dict) -> None:
        if not self.redis_url:
            return
        try:
            r = aioredis.from_url(self.redis_url, decode_responses=True)
            try:
                key = f"{REFLECTION_KEY_PREFIX}{goal_id}"
                existing_raw = await r.get(key)
                existing: list = json.loads(existing_raw) if existing_raw else []
                existing.append(entry)
                # Cap at max
                existing = existing[-MAX_REFLECTIONS_PER_GOAL:]
                await r.set(key, json.dumps(existing), ex=REFLECTION_TTL_SECONDS)
            finally:
                await r.aclose()
        except Exception as exc:
            logger.debug("reflection_engine.save_failed", error=str(exc)[:120])

    async def _register_recent_goal(self, goal_id: str) -> None:
        if not self.redis_url:
            return
        try:
            r = aioredis.from_url(self.redis_url, decode_responses=True)
            try:
                raw = await r.get(REFLECTION_RECENT_KEY)
                recent: list = json.loads(raw) if raw else []
                # Prepend, dedup, cap
                if goal_id in recent:
                    recent.remove(goal_id)
                recent.insert(0, goal_id)
                recent = recent[:RECENT_GOAL_LIST_SIZE]
                await r.set(REFLECTION_RECENT_KEY, json.dumps(recent), ex=REFLECTION_TTL_SECONDS)
            finally:
                await r.aclose()
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────
# Context formatting (for build_context injection)
# ──────────────────────────────────────────────────────────────────────────

def format_reflections_for_context(reflections: list[dict]) -> str:
    """Format goal reflections as a system prompt block."""
    if not reflections:
        return ""
    lines = ["[REFLEXIONES APRENDIDAS — insights de ejecuciones anteriores:]"]
    for i, r in enumerate(reflections, 1):
        outcome_icon = "✓" if r.get("outcome") == "success" else "✗"
        lines.append(f"  {i}. [{outcome_icon}] {r.get('reflection', '')}")
    return "\n".join(lines)


# ── ─────────────────────────────────────────────────────────────────────────
# Execution Reflection Engine (heuristic, DB-backed)
# ─────────────────────────────────────────────────────────────────────────────

def _classify_reflection(
    success: bool,
    issues: list[str],
    recurring: bool,
) -> str:
    """Return reflection type: error | efficiency | pattern | optimization.

    Change 2 — deterministic classification for future filtering/analytics.
    """
    if not success:
        return "error"
    if recurring:
        return "pattern"
    if any(i in issues for i in ("slow_execution", "moderate_latency", "high_skill_count", "redundant_skills")):
        return "efficiency"
    return "optimization"


def _heuristic_reflect(
    intent: str,
    skills_used: list[str],
    duration_ms: int,
    success: bool,
    error: str,
    retries: int,
) -> dict:
    """Generate a deterministic heuristic reflection without any LLM call.

    Change 3: enhanced signal detection — redundant skills, instability,
    high duration.  Always returns a valid dict — never raises.
    """
    issues: list[str] = []
    efficiency = 1.0

    # Duration buckets
    if duration_ms > 30_000:
        issues.append("slow_execution")
        efficiency -= 0.30
    elif duration_ms > 10_000:
        issues.append("moderate_latency")
        efficiency -= 0.10

    # Skill complexity + redundancy
    unique_skills = sorted(set(skills_used))
    if len(unique_skills) > 4:
        issues.append("high_skill_count")
        efficiency -= 0.10
    elif len(skills_used) > 2 and len(unique_skills) < len(skills_used):
        # Same skill called multiple times — likely redundant
        issues.append("redundant_skills")
        efficiency -= 0.05

    # Outcome + retries
    if not success:
        issues.append("execution_failed")
        efficiency -= 0.40
    if error:
        issues.append("has_error")
    if retries > 2:
        # ≥3 retries = instability, not just a one-off hiccup
        issues.append("instability")
        efficiency -= 0.10 * min(retries, 3)
    elif retries > 0:
        issues.append("required_retries")
        efficiency -= 0.05 * min(retries, 2)

    efficiency = round(max(0.0, min(1.0, efficiency)), 2)

    # Change 3: improved insight text — specific, actionable, one sentence
    skill_summary = ", ".join(unique_skills[:3]) if unique_skills else "no skills"
    if not success and error:
        insight    = f"Execution failed: {error[:80].rstrip('., ')}"
        suggestion = "Review error handling or skill selection for this pattern"
    elif "instability" in issues:
        insight    = f"Repeated retries ({retries}) indicate instability with [{skill_summary}]"
        suggestion = "Investigate root cause — this pattern fails consistently"
    elif "redundant_skills" in issues:
        insight    = f"Same skill called multiple times — possible redundant execution via [{skill_summary}]"
        suggestion = "Check skill call logic; one invocation may suffice"
    elif len(skills_used) > 4 and success:
        insight    = f"Used {len(skills_used)} skills where fewer might suffice for this intent"
        suggestion = "Consider a more direct skill path for similar requests"
    elif duration_ms > 30_000:
        insight    = f"Execution took {duration_ms // 1000}s via [{skill_summary}] — above threshold"
        suggestion = "Consider parallelising or caching for similar requests"
    elif duration_ms > 10_000 and retries > 0:
        insight    = f"Moderate latency ({duration_ms // 1000}s) with {retries} retr{'y' if retries == 1 else 'ies'}"
        suggestion = "First-attempt accuracy could reduce total latency"
    else:
        insight    = f"Completed via [{skill_summary}] in {duration_ms}ms"
        suggestion = "Stable pattern — reuse for similar intents"

    # Canonical pattern key: top-3 unique skills (sorted) + outcome
    skill_key   = "+".join(unique_skills[:3]) if unique_skills else "direct"
    outcome_key = "ok" if success else "fail"
    pattern_key = f"{skill_key}:{outcome_key}"[:100]

    return {
        "intent":           intent[:150],
        "success":          success,
        "efficiency_score": efficiency,
        "issues":           issues,
        "insight":          insight,
        "suggestion":       suggestion,
        "pattern_key":      pattern_key,
    }


async def reflect_on_execution(
    intent: str,
    skills_used: list[str],
    duration_ms: int,
    success: bool,
    error: str = "",
    retries: int = 0,
    chat_id: str = "",
) -> bool:
    """Generate a heuristic reflection and persist it to execution_reflections.

    Fire-and-forget safe — all exceptions are caught and logged.
    Returns True if stored, False if skipped/failed.

    Change 1: trivial-success filter — clean fast executions are not stored.
    Change 2: reflection_type field added.
    """
    # Skip trivially empty turns (scheduled ticks, no-op heartbeats)
    if not intent and not skills_used:
        return False

    try:
        from .db.session import async_session
        from .db.models import ExecutionReflection
        from sqlalchemy import select, func

        data = _heuristic_reflect(intent, skills_used, duration_ms, success, error, retries)

        # Filter trivial reflections: only skip single-skill fast successes with no issues.
        # Multi-skill interactions and retried executions always carry learning signal.
        _is_trivial = (
            success
            and data["efficiency_score"] > 0.8
            and not data["issues"]
            and len(skills_used) <= 1    # single-skill calls have no coordination signal
            and retries == 0             # any retry indicates something worth recording
        )
        if _is_trivial:
            logger.debug(
                "execution_reflection.skipped_trivial",
                pattern_key=data["pattern_key"],
                efficiency=data["efficiency_score"],
                skills=len(skills_used),
            )
            return False

        async with async_session() as session:
            # Pattern detection: how many times has this pattern_key appeared?
            count_stmt = select(func.count(ExecutionReflection.id)).where(
                ExecutionReflection.pattern_key == data["pattern_key"]
            )
            pattern_count = (await session.execute(count_stmt)).scalar() or 0
            recurring = pattern_count >= (EXEC_PATTERN_THRESHOLD - 1)

            # Change 2 — classify reflection type
            reflection_type = _classify_reflection(
                success=data["success"],
                issues=data["issues"],
                recurring=recurring,
            )

            entry = ExecutionReflection(
                id=str(uuid4()),
                chat_id=chat_id,
                intent=data["intent"],
                skills_used=skills_used,
                duration_ms=duration_ms,
                success=data["success"],
                efficiency_score=data["efficiency_score"],
                issues=data["issues"],
                insight=data["insight"],
                suggestion=data["suggestion"],
                pattern_key=data["pattern_key"],
                recurring_pattern=recurring,
                reflection_type=reflection_type,
            )
            session.add(entry)
            await session.commit()

        logger.debug(
            "execution_reflection.stored",
            pattern_key=data["pattern_key"],
            efficiency=data["efficiency_score"],
            type=reflection_type,
            recurring=recurring,
            chat_id=chat_id,
        )
        if recurring:
            logger.info(
                "execution_reflection.recurring_pattern",
                pattern_key=data["pattern_key"],
                type=reflection_type,
                occurrences=pattern_count + 1,
            )
        return True

    except Exception as exc:
        logger.debug("execution_reflection.store_error", error=str(exc)[:120])
        return False


def _keyword_overlap(a: str, b: str) -> float:
    """Return word-overlap ratio between two strings (0.0–1.0).

    Change 4 helper — no imports, no external libs, deterministic.
    Splits on whitespace, lowercases, ignores stop-words.
    """
    stop = _KEYWORD_OVERLAP_STOP
    wa = {w.lower().strip(".,!?\"'") for w in a.split() if len(w) > 2} - stop
    wb = {w.lower().strip(".,!?\"'") for w in b.split() if len(w) > 2} - stop
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))


async def get_execution_reflections(
    chat_id: str = "",
    limit: int = 3,
    current_intent: str = "",
) -> list[dict]:
    """Retrieve relevant execution reflections for context injection.

    Change 4: intent similarity filter — pulls a broader candidate pool,
    scores each by keyword overlap with current_intent, returns the top
    matches.  Falls back to recency order when no candidates match.

    Prioritises: recurring patterns, then similarity score, then recency.
    """
    _SIMILARITY_THRESHOLD = 0.25   # min overlap to be considered relevant
    _CANDIDATE_POOL       = 20     # rows to fetch before similarity ranking

    try:
        from .db.session import async_session
        from .db.models import ExecutionReflection
        from sqlalchemy import select

        async with async_session() as session:
            stmt = select(ExecutionReflection)
            if chat_id:
                stmt = stmt.where(ExecutionReflection.chat_id == chat_id)
            stmt = stmt.order_by(
                ExecutionReflection.recurring_pattern.desc(),
                ExecutionReflection.timestamp.desc(),
            ).limit(_CANDIDATE_POOL)
            rows = (await session.execute(stmt)).scalars().all()

        if not rows:
            return []

        candidates = [
            {
                "insight":          r.insight,
                "suggestion":       r.suggestion,
                "success":          r.success,
                "efficiency_score": r.efficiency_score,
                "pattern_key":      r.pattern_key,
                "recurring":        r.recurring_pattern,
                "reflection_type":  getattr(r, "reflection_type", "optimization"),
                "timestamp":        r.timestamp.isoformat() if r.timestamp else "",
                "_intent":          r.intent,  # used for similarity, stripped before return
            }
            for r in rows
        ]

        # Micro Adjustment 1: always pin the most recent error reflection
        pinned_error = None
        for c in candidates:
            if not c["success"] or c["reflection_type"] == "error":
                pinned_error = c
                break  # candidates already ordered by timestamp desc → first match is most recent

        # Change 4: similarity ranking when a current intent is provided
        remaining_limit = limit - (1 if pinned_error else 0)
        non_error_candidates = [c for c in candidates if c is not pinned_error]

        if current_intent:
            for c in non_error_candidates:
                c["_score"] = _keyword_overlap(current_intent, c["_intent"])

            # Split: matched vs unmatched
            matched   = [c for c in non_error_candidates if c["_score"] >= _SIMILARITY_THRESHOLD]

            # Sort matched by (recurring, score); fall back to full list if nothing matches
            if matched:
                matched.sort(key=lambda c: (c["recurring"], c["_score"]), reverse=True)
                ranked = matched[:remaining_limit]
            else:
                ranked = non_error_candidates[:remaining_limit]   # fallback: recency order
        else:
            ranked = non_error_candidates[:remaining_limit]

        # Prepend pinned error so it always appears first
        if pinned_error:
            ranked = [pinned_error] + ranked

        # Strip internal scoring fields before returning
        for c in ranked:
            c.pop("_intent", None)
            c.pop("_score", None)
        return ranked

    except Exception as exc:
        logger.debug("execution_reflection.get_error", error=str(exc)[:120])
        return []


def format_execution_reflections_for_context(entries: list[dict]) -> str:
    """Format execution reflections as a compact system prompt block.

    Change 5:
    - Skip entries with empty or trivially weak insight (<15 chars)
    - One sentence per line: insight only (suggestion omitted when generic)
    - Max 3 lines
    - Returns "" when nothing meaningful remains
    """
    _GENERIC_SUGGESTIONS = frozenset({
        "No immediate optimisation required",
        "Stable pattern — reuse for similar intents",
    })
    _MIN_INSIGHT_LEN = 15

    meaningful = [
        r for r in entries
        if len(r.get("insight", "")) >= _MIN_INSIGHT_LEN
    ]
    if not meaningful:
        return ""

    lines = ["[Recent execution insights:]"]
    for r in meaningful[:3]:
        icon       = "✓" if r.get("success") else "✗"
        rtype      = r.get("reflection_type", "")
        rec_suffix = " ↻" if r.get("recurring") else ""
        suggestion = r.get("suggestion", "")

        # Include suggestion only when it adds information beyond the insight
        if suggestion and suggestion not in _GENERIC_SUGGESTIONS:
            line = f"  • [{icon}{rtype[:3].upper() if rtype else ''}] {r['insight']} → {suggestion}{rec_suffix}"
        else:
            line = f"  • [{icon}] {r['insight']}{rec_suffix}"

        # Hard cap: one sentence, ≤120 chars per bullet
        if len(line) > 125:
            line = line[:122] + "…"
        lines.append(line)

    return "\n".join(lines)
