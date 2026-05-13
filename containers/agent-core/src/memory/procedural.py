"""Procedural Memory — abstracts successful multi-step solutions into reusable procedures.

When the agent solves a complex task (>2 skill rounds, multiple unique skills),
this system extracts the "how" and stores it as a named procedure.
Future similar tasks can retrieve and adapt the procedure instead of solving from scratch.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from uuid import uuid4

import structlog
from sqlalchemy import select, func

from ..db.session import async_session

logger = structlog.get_logger()

# Minimum complexity to abstract a procedure
MIN_ROUNDS_FOR_ABSTRACTION = 2
MIN_UNIQUE_SKILLS = 2

# Patterns in user input that indicate a complaint / correction turn — do NOT learn from these
_COMPLAINT_RE = re.compile(
    r"\b(?:"
    r"alucinas?|alucinando|deja\s+de|por\s+qué\s+(?:no|dices)|eso\s+está\s+mal|"
    r"te\s+estoy\s+pidiendo|no\s+aprendes?|funciona(?:ndo)?\s+(?:peor|mal)|"
    r"repites\s+el\s+error|de\s+nuevo\s+(?:el|lo\s+mismo)|constantemente|"
    # Expanded set covering the failure phrases observed in the May 2026 dialog
    r"estamos\s+mal|esto\s+no\s+funciona|no\s+funciona|nada\s+que\s+ver|"
    r"(?:no\s+es|esto\s+no\s+es)\s+lo\s+que\s+(?:te\s+)?ped[ií]|"
    r"te\s+lo\s+ped[ií]|no\s+te\s+ped[ií]|por\s+qu[eé]\s+(?:respondes|insistes|haces)|"
    r"no\s+memorizas?|no\s+recuerdas?|(?:no\s+)?aprendes\s+nada|"
    r"sigues?\s+(?:fallando|igual)|otra\s+vez\s+lo\s+mismo|"
    # English mirrors
    r"hallucinating|stop\s+(?:doing|sending|saying)|why\s+(?:do\s+you|are\s+you)|"
    r"that'?s\s+wrong|you'?re\s+(?:wrong|broken|failing)|"
    r"that'?s\s+not\s+what|that'?s\s+unrelated|you\s+don'?t\s+remember"
    r")\b",
    re.IGNORECASE,
)


def _sequence_had_failures(skill_sequence: list[dict]) -> bool:
    """Return True if any step output indicates the tool failed or was blocked.

    We look at output_summary for known failure markers. This catches:
      - pre_execution_guard blocks ("active_lock", "blocked", "domain_lock")
      - browser failures ("CAPTURE_VALID: false", "page blocked", "Invalid capture")
      - generic error envelopes ("[error]", "skill error:")
    """
    _FAIL_MARKERS = (
        "[capture_valid: false]", "invalid capture", "page blocked",
        "active_lock=", "blocked_skills=", "pre_execution_guard.blocked",
        "domain_lock.blocked_mismatch", "[error]", "skill error:",
        "execution_failed", "tool_violation",
    )
    for step in skill_sequence:
        out = (step.get("output_summary") or "").lower()
        if not out:
            continue
        if any(marker in out for marker in _FAIL_MARKERS):
            return True
    return False


def _has_repeated_skill_calls(skill_sequence: list[dict]) -> bool:
    """A retry pattern means the procedure is brittle — don't abstract it."""
    seen = []
    for step in skill_sequence:
        sig = (step.get("skill_name", ""), str(step.get("arguments", {}))[:120])
        if sig in seen:
            return True
        seen.append(sig)
    return False


async def abstract_procedure(
    user_input: str,
    skill_sequence: list[dict],  # [{skill_name, arguments, output_summary}]
    final_outcome: str,
    chat_id: str,
    model_manager=None,
) -> str | None:
    """
    Given a successful multi-step skill sequence, ask LLM to abstract it
    into a reusable named procedure. Returns procedure id or None if skipped.
    """
    if len(skill_sequence) < MIN_UNIQUE_SKILLS:
        return None
    unique_skills = {s["skill_name"] for s in skill_sequence}
    if len(unique_skills) < MIN_UNIQUE_SKILLS:
        return None

    # Do NOT abstract procedures from complaint/correction exchanges — they encode bad behavior
    if _COMPLAINT_RE.search(user_input):
        logger.debug("procedural_memory.abstraction_skipped_complaint", input_preview=user_input[:60])
        return None

    # Do NOT abstract if the outcome looks like an error or hallucination report
    if final_outcome and _COMPLAINT_RE.search(final_outcome):
        return None

    # Do NOT abstract if any tool in the sequence failed or was blocked.
    # Bad procedures like "context_memory_reassurance" or "track_shipment_status"
    # were learned because the gate didn't notice domain_lock blocks in step output.
    if _sequence_had_failures(skill_sequence):
        logger.debug(
            "procedural_memory.abstraction_skipped_tool_failure",
            input_preview=user_input[:60],
            steps=len(skill_sequence),
        )
        return None

    # Do NOT abstract if the same skill+arguments appears more than once.
    # A retry means the first attempt failed, the procedure is unstable.
    if _has_repeated_skill_calls(skill_sequence):
        logger.debug(
            "procedural_memory.abstraction_skipped_retries",
            input_preview=user_input[:60],
            steps=len(skill_sequence),
        )
        return None

    # Build description for LLM
    steps_text = "\n".join(
        f"{i+1}. {s['skill_name']}({json.dumps(s.get('arguments', {}))[:200]})"
        for i, s in enumerate(skill_sequence)
    )
    prompt = f"""Analyze this successful agent task and extract a reusable procedure.

USER REQUEST: {user_input[:300]}

SKILL SEQUENCE USED:
{steps_text}

FINAL OUTCOME: {final_outcome[:300]}

Extract a procedure with:
1. A short name (3-5 words, snake_case)
2. A description (1 sentence)
3. 3-6 keywords that would trigger this procedure (comma separated)
4. The abstract steps (generalized, without specific values)

Respond ONLY as JSON:
{{
  "name": "fetch_and_analyze_webpage",
  "description": "Navigate to a URL, extract content, and answer a specific question about it",
  "keywords": ["fetch url", "analyze page", "extract content", "web information"],
  "steps": [
    "Use browser or fetch_url to retrieve the target URL",
    "Extract the relevant section or data from the page content",
    "Analyze or summarize based on the user's specific question"
  ]
}}

If this task is too specific or trivial to generalize, respond with: {{"skip": true}}"""

    try:
        if model_manager is None:
            return None
        from ..models.types import Message, ModelRequest
        request = ModelRequest(messages=[
            Message(role="system", content="You are a procedure abstraction engine. Extract reusable patterns from successful agent tasks. Always respond with valid JSON only."),
            Message(role="user", content=prompt),
        ])
        response = await model_manager.generate(request)
        text = response.content.strip()
        # Extract JSON from possible markdown fences
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        data = json.loads(text.strip())
        if data.get("skip"):
            return None

        # ── Phase 6.2: Content filter before DB write ─────────────────────────
        from ..events.procedural_filter import scan_procedural_content
        _safe, _reason = scan_procedural_content(data)
        if not _safe:
            logger.warning(
                "procedural_memory.abstraction_rejected_unsafe",
                reason=_reason,
                name=data.get("name", "")[:60],
            )
            return None
        # ── End Phase 6.2 filter ──────────────────────────────────────────────

        proc_id = str(uuid4())
        async with async_session() as session:
            from ..db.models import ProceduralMemory
            proc = ProceduralMemory(
                id=proc_id,
                name=data.get("name", "unnamed_procedure")[:200],
                description=data.get("description", "")[:500],
                trigger_keywords=data.get("keywords", [])[:20],
                steps=data.get("steps", []),
                source_chat_id=chat_id,
            )
            session.add(proc)
            await session.commit()
        logger.info("procedural_memory.abstracted", name=data.get("name"), id=proc_id)
        return proc_id
    except Exception as e:
        logger.debug("procedural_memory.abstraction_failed", error=str(e))
        return None


async def find_procedures(task_description: str, limit: int = 3) -> list[dict]:
    """Find matching procedures using hybrid retrieval.

    Strategy:
      1. Semantic similarity search via vector memory (if enabled + Ollama available)
      2. Keyword fallback if semantic returns empty results

    Returns top-N ranked by similarity score (semantic) or keyword overlap + success_count.
    """
    # ── Path 1: Semantic search ───────────────────────────────────────────────
    try:
        from ..config import settings as _cfg
        if _cfg.vector_memory_enabled:
            semantic_results = await _find_procedures_semantic(task_description, limit)
            if semantic_results:
                logger.info(
                    "memory_retrieved",
                    memory_type="procedural",
                    retrieval_method="semantic",
                    count=len(semantic_results),
                )
                return semantic_results
    except Exception as _se:
        logger.debug("procedural.semantic_search_failed", error=str(_se)[:120])

    # ── Path 2: Keyword fallback ──────────────────────────────────────────────
    logger.debug("procedural.using_keyword_fallback")
    return await _find_procedures_keyword(task_description, limit)


async def _find_procedures_semantic(task_description: str, limit: int) -> list[dict]:
    """Semantic similarity search over procedural memory via vector embeddings."""
    from ..config import settings as _cfg
    from ..memory.vector_memory import semantic_search
    from ..memory.embeddings import create_provider as _make_provider
    from ..db.session import async_session

    async with async_session() as session:
        results = await semantic_search(
            session=session,
            query=task_description[:1000],
            provider=_make_provider(_cfg),
            source_type="procedural",
            top_k=limit * 2,
        )

    if not results:
        return []

    # Resolve source_ids → ProceduralMemory rows
    source_ids = [r["source_id"] for r in results]
    async with async_session() as session:
        from ..db.models import ProceduralMemory
        from sqlalchemy import select
        rows = (await session.execute(
            select(ProceduralMemory).where(ProceduralMemory.id.in_(source_ids))
        )).scalars().all()

    id_to_row = {r.id: r for r in rows}
    matched = []
    for res in results:
        proc = id_to_row.get(res["source_id"])
        if proc:
            matched.append({
                "id": proc.id,
                "name": proc.name,
                "description": proc.description,
                "keywords": proc.trigger_keywords,
                "steps": proc.steps,
                "success_count": proc.success_count,
                "score": res["score"],
                "retrieval_method": "semantic",
            })

    return matched[:limit]


async def _find_procedures_keyword(task_description: str, limit: int) -> list[dict]:
    """Original keyword-based procedural memory retrieval (fallback)."""
    task_lower = task_description.lower()
    try:
        async with async_session() as session:
            from ..db.models import ProceduralMemory
            result = await session.execute(
                select(ProceduralMemory)
                .order_by(ProceduralMemory.success_count.desc(), ProceduralMemory.last_used_at.desc().nullslast())
                .limit(50)
            )
            all_procs = result.scalars().all()

        # Minimum 2 keywords must match to avoid accidental triggering on
        # generic/common words (e.g. a procedure with "weather" fires on "clima" complaints)
        _MIN_KEYWORD_MATCHES = 2
        matches = []
        for proc in all_procs:
            keywords = proc.trigger_keywords or []
            score = sum(1 for kw in keywords if kw.lower() in task_lower)
            if score >= _MIN_KEYWORD_MATCHES:
                matches.append((score, proc))

        matches.sort(key=lambda x: x[0], reverse=True)
        logger.info(
            "memory_retrieved",
            memory_type="procedural",
            retrieval_method="keyword",
            count=len(matches[:limit]),
        )
        return [
            {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "keywords": p.trigger_keywords,
                "steps": p.steps,
                "success_count": p.success_count,
                "score": score / max(len(p.trigger_keywords or []), 1),
                "retrieval_method": "keyword",
            }
            for score, p in matches[:limit]
        ]
    except Exception:
        return []


async def record_use(procedure_id: str, success: bool) -> None:
    """Update usage stats for a procedure."""
    try:
        async with async_session() as session:
            from ..db.models import ProceduralMemory
            proc = await session.get(ProceduralMemory, procedure_id)
            if proc:
                if success:
                    proc.success_count = (proc.success_count or 0) + 1
                else:
                    proc.failure_count = (proc.failure_count or 0) + 1
                proc.last_used_at = datetime.now(timezone.utc)
                await session.commit()
    except Exception:
        pass


def format_procedures_for_context(procedures: list[dict]) -> str:
    """Format matching procedures as context hint for LLM."""
    if not procedures:
        return ""
    lines = ["[PROCEDIMIENTOS CONOCIDOS — patrones que funcionaron antes:]"]
    for p in procedures:
        lines.append(f"\n• {p['name']}: {p['description']}")
        for i, step in enumerate(p.get("steps", [])[:5], 1):
            lines.append(f"  {i}. {step}")
    return "\n".join(lines)
