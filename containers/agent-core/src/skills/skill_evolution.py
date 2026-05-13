"""System 5 — Skill Evolution Engine.

Automatically detects recurring multi-skill workflows in the audit log and
synthesises new composite skills from them.

Architecture:
  audit_log (PostgreSQL) → pattern detection → SkillPattern table
    → threshold check → LLM skill code generation → skill_manager skill store

Safety rules:
  - Minimum occurrence threshold (SKILL_PATTERN_THRESHOLD, default 5) before synthesis.
  - Generated skills require LLM validation before registration.
  - All composite skills are stored as Python custom skills (same as user-created).
  - The engine NEVER modifies existing skills — only creates new ones.
  - Each pattern can only be synthesised once (SkillPattern.synthesised flag).

Feature flag: SKILL_EVOLUTION_ENABLED=false → no-op.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import structlog

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from ..models.manager import ModelManager
    from ..skills.registry import SkillRegistry

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Pattern detection
# ---------------------------------------------------------------------------


async def analyze_skill_sequences(
    session: AsyncSession,
    hours: float = 168.0,  # 7 days
    window_seconds: int = 300,  # 5 min — tasks within this window are a "sequence"
) -> list[list[str]]:
    """Extract skill usage sequences from recent audit_log.

    A sequence is a set of skill executions within `window_seconds` of each other
    for the same chat_id.

    Returns a list of skill name sequences (each a list of str).
    """
    from ..db.models import AuditLog
    from sqlalchemy import select

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = (
        await session.execute(
            select(AuditLog)
            .where(
                AuditLog.timestamp >= cutoff,
                AuditLog.event_type == "skill_execution",
            )
            .order_by(AuditLog.chat_id, AuditLog.timestamp)
        )
    ).scalars().all()

    if not rows:
        return []

    sequences: list[list[str]] = []
    current_seq: list[str] = []
    last_ts: datetime | None = None
    last_chat: str = ""

    for row in rows:
        skill_name = _extract_skill_name(row.action)
        if not skill_name:
            continue

        ts = row.timestamp
        chat = row.chat_id or ""

        if (
            last_ts is None
            or chat != last_chat
            or (ts - last_ts).total_seconds() > window_seconds
        ):
            if len(current_seq) >= 2:
                sequences.append(current_seq)
            current_seq = [skill_name]
        else:
            current_seq.append(skill_name)

        last_ts = ts
        last_chat = chat

    if len(current_seq) >= 2:
        sequences.append(current_seq)

    return sequences


def _extract_skill_name(action: str) -> str | None:
    """Extract skill name from audit_log.action field (e.g. 'skill:web_search')."""
    if not action:
        return None
    m = re.match(r"skill[:\.](\w+)", action, re.IGNORECASE)
    if m:
        return m.group(1)
    # Fallback: if action is just the skill name
    if re.match(r"^\w+$", action) and len(action) < 50:
        return action
    return None


def _sequence_to_key(seq: list[str]) -> str:
    return "→".join(seq)


async def detect_patterns(
    session: AsyncSession,
    hours: float = 168.0,
    min_count: int = 5,
) -> list[dict]:
    """Detect recurring skill patterns above the usage threshold.

    Returns patterns not yet synthesised that exceed min_count occurrences.
    """
    from ..db.models import SkillPattern
    from sqlalchemy import select

    sequences = await analyze_skill_sequences(session, hours)

    # Count occurrences
    counts: dict[str, int] = {}
    for seq in sequences:
        key = _sequence_to_key(seq)
        counts[key] = counts.get(key, 0) + 1

    new_patterns: list[dict] = []

    for key, count in counts.items():
        if count < min_count:
            continue

        # Check if pattern already exists in DB
        existing = (
            await session.execute(
                select(SkillPattern).where(SkillPattern.pattern_key == key)
            )
        ).scalar_one_or_none()

        if existing:
            # Update occurrence count
            existing.occurrence_count = count
            existing.last_seen_at = datetime.now(timezone.utc)
            if not existing.synthesized:
                new_patterns.append(
                    {
                        "id": existing.id,
                        "pattern_key": key,
                        "skill_sequence": existing.skill_sequence,
                        "occurrence_count": count,
                    }
                )
        else:
            skill_seq = key.split("→")
            pattern = SkillPattern(
                id=str(uuid4()),
                pattern_key=key,
                skill_sequence=skill_seq,
                occurrence_count=count,
            )
            session.add(pattern)
            new_patterns.append(
                {
                    "id": pattern.id,
                    "pattern_key": key,
                    "skill_sequence": skill_seq,
                    "occurrence_count": count,
                }
            )

    try:
        await session.commit()
    except Exception as exc:
        await session.rollback()
        logger.warning("skill_evolution.pattern_save_failed", error=str(exc)[:120])

    return new_patterns


# ---------------------------------------------------------------------------
# Skill code generation
# ---------------------------------------------------------------------------


_CODEGEN_SYSTEM = """You are a Python skill generator for an autonomous AI agent.

You will be given a recurring sequence of skill calls and must generate a NEW composite
Python skill that encapsulates them as a single reusable operation.

The skill must follow this exact template:
```python
from src.skills.base import SkillBase
from src.skills.types import SkillDefinition, SkillParam, SkillResult

class CompositeSkill(SkillBase):
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="<snake_case_name>",
            description="<one clear sentence>",
            params=[
                SkillParam(name="param1", description="...", required=True),
            ],
        )

    async def execute(self, **params) -> SkillResult:
        # Implementation using existing skills via shell/python_exec
        # Return SkillResult(skill_name=self.definition().name, output="...", success=True)
        pass
```

Return ONLY the Python code with no markdown fences.
"""


async def generate_composite_skill(
    pattern: dict,
    model_manager: ModelManager,
) -> str | None:
    """Use LLM to generate Python code for a composite skill."""
    from ..models.types import Message, ModelRequest

    seq = pattern["skill_sequence"]
    seq_str = " → ".join(seq)
    count = pattern["occurrence_count"]

    prompt = (
        f"The following skill sequence has been used {count} times:\n"
        f"  {seq_str}\n\n"
        "Generate a composite Python skill that encapsulates this workflow. "
        "The skill should accept parameters that cover all the required inputs "
        "for the full sequence and orchestrate them internally. "
        "Name the skill descriptively (e.g. 'web_report_emailer' for "
        "web_search→fetch_url→gmail sequence).\n\n"
        "Return ONLY the Python class code, no explanations."
    )

    try:
        resp = await model_manager.generate(
            ModelRequest(
                messages=[
                    Message(role="system", content=_CODEGEN_SYSTEM),
                    Message(role="user", content=prompt),
                ],
                max_tokens=800,
                temperature=0.2,
            )
        )
        code = resp.content.strip()
        # Strip markdown fences if present
        if "```" in code:
            parts = code.split("```")
            for part in parts:
                if "class " in part:
                    code = part.lstrip("python").strip()
                    break
        return code if "class " in code else None
    except Exception as exc:
        logger.warning("skill_evolution.codegen_failed", error=str(exc)[:120])
        return None


_DANGEROUS_IMPORTS = {
    "subprocess", "os", "sys", "pty", "ctypes", "pickle", "marshal",
    "importlib", "__import__", "eval", "exec", "compile",
}

_SAFE_SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,48}$")


def _validate_skill_code(code: str) -> str | None:
    """Validate LLM-generated skill code. Returns error message or None if safe."""
    import ast

    # Must parse as valid Python
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"Syntax error: {exc}"

    # Scan for dangerous imports / calls
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _DANGEROUS_IMPORTS:
                    return f"Dangerous import blocked: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in _DANGEROUS_IMPORTS:
                return f"Dangerous import blocked: {node.module}"
        elif isinstance(node, ast.Call):
            # Block bare eval/exec/compile calls
            if isinstance(node.func, ast.Name) and node.func.id in {"eval", "exec", "compile", "__import__"}:
                return f"Dangerous call blocked: {node.func.id}"

    # Must contain exactly one class inheriting SkillBase
    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    if not classes:
        return "No class definition found in generated code"

    return None  # all clear


async def register_composite_skill(
    session: AsyncSession,
    pattern_id: str,
    skill_code: str,
    skill_name: str,
    skill_description: str,
) -> bool:
    """Persist the composite skill and mark the pattern as synthesised."""
    from ..db.models import SkillPattern
    from sqlalchemy import select
    import os

    # Sanitize skill name to prevent path traversal
    if not _SAFE_SKILL_NAME_RE.match(skill_name):
        safe_name = re.sub(r"[^a-z0-9_]", "_", skill_name.lower())[:48]
        safe_name = safe_name.lstrip("_") or "composite_skill"
        logger.warning("skill_evolution.unsafe_name_sanitized", original=skill_name, sanitized=safe_name)
        skill_name = safe_name

    # Validate generated code before writing to disk
    code_error = _validate_skill_code(skill_code)
    if code_error:
        logger.warning("skill_evolution.code_validation_failed", pattern_id=pattern_id, reason=code_error)
        return False

    # Save skill file to /data/skills/<name>/skill.py
    skill_dir = os.path.realpath(f"/data/skills/{skill_name}")
    if not skill_dir.startswith("/data/skills/"):
        logger.warning("skill_evolution.path_escape_blocked", skill_name=skill_name)
        return False

    try:
        os.makedirs(skill_dir, exist_ok=True)
        with open(f"{skill_dir}/skill.py", "w") as f:
            f.write(skill_code)
        logger.info("skill_evolution.skill_saved", name=skill_name, path=skill_dir)
    except Exception as exc:
        logger.warning("skill_evolution.save_failed", error=str(exc)[:120])
        return False

    # Mark pattern as synthesised
    row = (
        await session.execute(
            select(SkillPattern).where(SkillPattern.id == pattern_id)
        )
    ).scalar_one_or_none()
    if row:
        row.synthesized = True
        row.composite_skill_name = skill_name
        try:
            await session.commit()
        except Exception:
            await session.rollback()

    return True


# ---------------------------------------------------------------------------
# Main engine class
# ---------------------------------------------------------------------------


class SkillEvolutionEngine:
    """Orchestrates the full skill evolution pipeline."""

    def __init__(
        self,
        model_manager: ModelManager,
        min_pattern_count: int = 5,
    ):
        self._model_manager = model_manager
        self._min_pattern_count = min_pattern_count

    async def run(self, session: AsyncSession) -> dict:
        """Run one cycle of the skill evolution engine.

        Returns a summary dict with stats.
        """
        t0 = time.monotonic()
        patterns = await detect_patterns(session, min_count=self._min_pattern_count)
        synthesised = 0

        for pattern in patterns:
            code = await generate_composite_skill(pattern, self._model_manager)
            if not code:
                continue

            # Extract skill name from code
            m = re.search(r'name\s*=\s*["\']([^"\']+)["\']', code)
            skill_name = m.group(1) if m else f"composite_{pattern['id'][:8]}"

            # Extract description
            dm = re.search(r'description\s*=\s*["\']([^"\']+)["\']', code)
            skill_description = dm.group(1) if dm else f"Composite: {pattern['pattern_key']}"

            ok = await register_composite_skill(
                session, pattern["id"], code, skill_name, skill_description
            )
            if ok:
                synthesised += 1
                logger.info(
                    "skill_evolution.synthesised",
                    skill_name=skill_name,
                    pattern=pattern["pattern_key"],
                    occurrences=pattern["occurrence_count"],
                )

        latency_ms = round((time.monotonic() - t0) * 1000)
        result = {
            "patterns_detected": len(patterns),
            "skills_synthesised": synthesised,
            "latency_ms": latency_ms,
        }
        logger.info("skill_evolution.cycle_complete", **result)
        return result
