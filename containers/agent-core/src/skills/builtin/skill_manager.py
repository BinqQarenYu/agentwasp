"""Skill Manager — create, enable, disable, list, and delete skills at runtime.

The agent can:
- Create custom skills:
  - Text skills (SKILL.md) — injected as prompt hints, no code execution
  - Python skills (skill.py) — real executable code registered in SkillRegistry
- Enable/disable built-in skills (via SkillRegistry) or custom skills (via .disabled marker)
- List all skills with their status
- Delete custom skills

Security: Python skills are scanned with the same two-layer regex+AST safety
check that python_exec uses, BEFORE the file is written.  Dangerous code is
rejected at create/edit time so it never reaches the live registry.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

import structlog

from ..base import SkillBase
from ..registry import SkillRegistry
from ..types import ParamType, SkillDefinition, SkillParam, SkillResult
from ..openclaw.loader import get_skills_dir, load_installed_skills

logger = structlog.get_logger()


def _slugify(name: str) -> str:
    """Convert a skill name to a filesystem-safe slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:50] or "custom-skill"


def _scan_python_code_safety(code: str) -> tuple[bool, str]:
    """Reuse python_exec's two-layer scanner so skill_manager-created skills
    cannot bypass the same safeguards.  Returns (safe, reason)."""
    try:
        from .python_exec import _scan_code_safety
        return _scan_code_safety(code)
    except Exception as e:
        # Fail-closed if scanner cannot be loaded — never silently accept code
        return False, f"safety scanner unavailable: {str(e)[:80]}"


async def _audit_skill_manager(
    action: str,
    skill_name: str,
    success: bool,
    detail: str,
    code_hash: str = "",
    block_reason: str = "",
) -> None:
    """Fire-and-forget audit log for skill_manager mutations."""
    try:
        from ...db.session import async_session
        from ...db.models import AuditLog
        async with async_session() as session:
            session.add(AuditLog(
                id=str(uuid.uuid4()),
                event_type=("skill_manager.blocked" if not success and block_reason
                            else f"skill_manager.{action}"),
                source="skill",
                action="skill.skill_manager",
                timestamp=datetime.now(timezone.utc),
                input_summary=f"action={action} name={skill_name}"[:400],
                output_summary=(
                    f"ok={success} hash={code_hash[:12]} {detail}"
                    + (f" block_reason={block_reason[:80]}" if block_reason else "")
                )[:400],
                user_id="",
                chat_id="",
                latency_ms=0,
                error=block_reason[:200] if block_reason else None,
            ))
            await session.commit()
    except Exception:
        pass


class SkillManagerSkill(SkillBase):
    """Manage skills: create, enable, disable, list, delete."""

    def __init__(self, registry: SkillRegistry):
        self._registry = registry

    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="skill_manager",
            description=(
                "Manage skills: create new custom skills, edit existing skills, "
                "enable/disable skills, list all skills, or delete custom skills."
            ),
            params=[
                SkillParam(
                    name="action",
                    description="Action: create, edit, enable, disable, list, delete",
                    required=True,
                ),
                SkillParam(
                    name="name",
                    description="Skill name (for enable/disable/delete/edit) or new skill name (for create)",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="description",
                    description="Short description of the skill (for create/edit)",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="instructions",
                    description="Detailed instructions for the skill — how to accomplish the task using shell, python_exec, fetch_url, etc. (for create/edit text skills)",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="code",
                    description=(
                        "Python code for the execute function body (for create python skills). "
                        "Include imports at the top and end with: return SkillResult(skill_name=name, success=True, output=str(result)). "
                        "The function receives **kwargs with skill parameters. "
                        "Available: import httpx, asyncio, json, subprocess, pathlib. "
                        "Example: result = httpx.get('https://api.example.com').text"
                    ),
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="params",
                    description=(
                        "Comma-separated list of parameter names for Python skills, e.g. 'query,limit'. "
                        "Each becomes a string parameter in the skill definition."
                    ),
                    required=False,
                    default="",
                ),
            ],
            category="system",
            timeout_seconds=10.0,
        )

    async def execute(self, **kwargs) -> SkillResult:
        action = kwargs.get("action", "list").lower().strip()
        name = kwargs.get("name", "").strip()

        try:
            if action == "create":
                return self._create(
                    name=name,
                    description=kwargs.get("description", "").strip(),
                    instructions=kwargs.get("instructions", "").strip(),
                    code=kwargs.get("code", "").strip(),
                    params=kwargs.get("params", "").strip(),
                )
            elif action == "edit":
                return self._edit(
                    name=name,
                    description=kwargs.get("description", "").strip(),
                    instructions=kwargs.get("instructions", "").strip(),
                    code=kwargs.get("code", "").strip(),
                )
            elif action == "enable":
                return self._enable(name)
            elif action == "disable":
                return self._disable(name)
            elif action == "list":
                return self._list()
            elif action == "delete":
                return self._delete(name)
            else:
                return SkillResult(
                    skill_name="skill_manager",
                    success=False,
                    output="",
                    error=f"Unknown action: {action}. Use: create, edit, enable, disable, list, delete",
                )
        except Exception as e:
            return SkillResult(
                skill_name="skill_manager",
                success=False,
                output="",
                error=str(e),
            )

    # ── create ───────────────────────────────────────────────────

    def _create(self, name: str, description: str, instructions: str, code: str = "", params: str = "") -> SkillResult:
        if not name:
            return SkillResult(
                skill_name="skill_manager", success=False, output="",
                error='Name required. Use: skill_manager(action="create", name="my-skill", description="...", code="...")',
            )

        slug = _slugify(name)
        skill_dir = get_skills_dir() / slug
        skill_dir.mkdir(parents=True, exist_ok=True)

        # Remove .disabled if it exists
        disabled_marker = skill_dir / ".disabled"
        if disabled_marker.exists():
            disabled_marker.unlink()

        if code:
            # Python skill: write skill.py and register live
            return self._create_python_skill(slug, name, description, code, params, skill_dir)
        elif instructions:
            # Text skill: write SKILL.md (prompt-injected hint)
            return self._create_text_skill(slug, name, description, instructions, skill_dir)
        else:
            return SkillResult(
                skill_name="skill_manager", success=False, output="",
                error=(
                    'Provide either:\n'
                    '  code="..." — Python code for the execute body (creates a real executable skill)\n'
                    '  instructions="..." — Text description (creates a prompt hint)'
                ),
            )

    def _create_text_skill(self, slug: str, name: str, description: str, instructions: str, skill_dir: Path) -> SkillResult:
        md_content = f"""---
name: {name}
description: {description or name}
version: "1.0.0"
metadata:
  openclaw:
    emoji: "🔧"
---

# Skill: {name}

{instructions}
"""
        (skill_dir / "SKILL.md").write_text(md_content, encoding="utf-8")
        logger.info("skill_manager.created_text", slug=slug, name=name)
        return SkillResult(
            skill_name="skill_manager",
            success=True,
            output=(
                f"Text skill '{name}' created and activated.\n"
                f"Location: /data/skills/{slug}/SKILL.md\n"
                f"This skill is injected as a hint into the system prompt.\n"
                f"Available from next message."
            ),
        )

    def _create_python_skill(self, slug: str, name: str, description: str, code: str, params: str, skill_dir: Path) -> SkillResult:
        """Write skill.py and dynamically register the skill in the live registry.

        Security gate: code is scanned with the same two-layer (regex + AST)
        safety scanner used by python_exec.  Code that would be blocked there
        is rejected here before any file is written or registry change made.
        """
        # ── Safety scan BEFORE writing anything ───────────────────────────────
        _safe, _reason = _scan_python_code_safety(code)
        if not _safe:
            code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()[:16]
            logger.warning(
                "skill_manager.python_blocked",
                slug=slug,
                name=name,
                reason=_reason,
                code_hash=code_hash,
            )
            asyncio.ensure_future(_audit_skill_manager(
                action="create_python", skill_name=name, success=False,
                detail="code rejected by safety scanner",
                code_hash=code_hash, block_reason=_reason,
            ))
            return SkillResult(
                skill_name="skill_manager",
                success=False,
                output="",
                error=(
                    f"⛔ Skill creation blocked: {_reason}. "
                    "The proposed code matches a sandbox-bypass pattern and was rejected. "
                    "Use higher-level skills (http_request, fetch_url, web_search, gmail) "
                    "or reformulate without subprocess/socket/ctypes/exec/eval."
                ),
            )

        code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()[:16]
        # Build params list
        param_names = [p.strip() for p in params.split(",") if p.strip()] if params else []
        params_code = ""
        if param_names:
            params_entries = ",\n                ".join(
                f'SkillParam(name="{p}", param_type=ParamType.STRING, description="{p}", required=False, default="")'
                for p in param_names
            )
            params_code = f"\n                {params_entries},"

        # Indent code body by 8 spaces (inside try block of execute())
        code_lines = code.splitlines()
        indented = "\n".join(f"            {line}" if line.strip() else "" for line in code_lines)

        skill_py_content = f'''"""Python skill: {name}

Auto-generated by skill_manager.
"""
import sys as _sys, os as _os
# Ensure /app is on sys.path so `src.*` imports work
_app_dir = "/app"
if _app_dir not in _sys.path:
    _sys.path.insert(0, _app_dir)

from src.skills.base import SkillBase
from src.skills.types import SkillDefinition, SkillParam, SkillResult, ParamType


class Skill(SkillBase):
    """Auto-generated skill: {name}"""

    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="{slug}",
            description={repr(description or name)},
            params=[{params_code}
            ],
        )

    async def execute(self, **kwargs) -> SkillResult:
        _skill_name = "{slug}"
        try:
{indented}
        except Exception as _e:
            return SkillResult(skill_name=_skill_name, success=False, output="", error=str(_e))
'''
        skill_py = skill_dir / "skill.py"
        skill_py.write_text(skill_py_content, encoding="utf-8")

        # Also write a minimal SKILL.md so the skill shows up in _list()
        md_content = f"---\nname: {name}\ndescription: {description or name}\ntype: python\n---\n\nPython skill.\n"
        (skill_dir / "SKILL.md").write_text(md_content, encoding="utf-8")

        # Dynamically load and register
        try:
            from ..openclaw.loader import load_python_skill
            instance = load_python_skill(skill_dir)
            if instance is not None:
                self._registry.register(instance)
                logger.info("skill_manager.created_python_live", slug=slug, name=name, code_hash=code_hash)
                asyncio.ensure_future(_audit_skill_manager(
                    action="create_python", skill_name=name, success=True,
                    detail=f"slug={slug} params={len(param_names)}",
                    code_hash=code_hash,
                ))
                return SkillResult(
                    skill_name="skill_manager",
                    success=True,
                    output=(
                        f"Python skill '{name}' created and registered live.\n"
                        f"Location: /data/skills/{slug}/skill.py\n"
                        f"Skill is immediately available as: {slug}(...)\n"
                        f"Parameters: {', '.join(param_names) if param_names else 'none (use **kwargs)'}"
                    ),
                )
            else:
                asyncio.ensure_future(_audit_skill_manager(
                    action="create_python", skill_name=name, success=False,
                    detail="failed to load Skill class",
                    code_hash=code_hash,
                ))
                return SkillResult(
                    skill_name="skill_manager",
                    success=False,
                    output=f"skill.py written to /data/skills/{slug}/ but failed to load.",
                    error="Check syntax: skill.py must define class Skill(SkillBase)",
                )
        except Exception as exc:
            asyncio.ensure_future(_audit_skill_manager(
                action="create_python", skill_name=name, success=False,
                detail=f"registration error: {str(exc)[:80]}",
                code_hash=code_hash,
            ))
            return SkillResult(
                skill_name="skill_manager",
                success=False,
                output=f"skill.py written to /data/skills/{slug}/ but registration failed.",
                error=str(exc),
            )

    # ── edit ────────────────────────────────────────────────────

    def _edit(self, name: str, description: str, instructions: str, code: str = "") -> SkillResult:
        if not name:
            return SkillResult(
                skill_name="skill_manager", success=False, output="",
                error='Name required. Use: skill_manager(action="edit", name="skill-name", description="...", instructions="...")',
            )

        # Find the custom skill directory
        slug = _slugify(name)
        skill_dir = get_skills_dir() / slug

        if not skill_dir.exists():
            # Try exact directory name match
            for entry in get_skills_dir().iterdir():
                if entry.is_dir() and entry.name.lower() == name.lower():
                    skill_dir = entry
                    slug = entry.name
                    break

        if not skill_dir.exists():
            return SkillResult(
                skill_name="skill_manager", success=False, output="",
                error=f"Skill '{name}' not found in /data/skills/. Only custom skills can be edited.",
            )

        # Read existing content to preserve fields not being updated
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            skill_md = skill_dir / "skill.md"
        if not skill_md.exists():
            return SkillResult(
                skill_name="skill_manager", success=False, output="",
                error=f"SKILL.md not found in /data/skills/{slug}/",
            )

        try:
            existing = skill_md.read_text(encoding="utf-8")
            existing_name_m = re.search(r"^name:\s*(.+)", existing, re.MULTILINE)
            existing_desc_m = re.search(r"^description:\s*(.+)", existing, re.MULTILINE)
            existing_name = existing_name_m.group(1).strip() if existing_name_m else name
            existing_desc = existing_desc_m.group(1).strip() if existing_desc_m else ""

            # Extract existing instructions
            body = re.sub(r"^---\s*\n.*?\n---\s*\n", "", existing, count=1, flags=re.DOTALL)
            body = re.sub(r"^#\s+Skill:.*\n*", "", body, count=1)
            existing_instructions = body.strip()
        except Exception:
            existing_name = name
            existing_desc = ""
            existing_instructions = ""

        # Use provided values or fall back to existing
        final_name = name if name != slug else existing_name
        final_desc = description or existing_desc
        final_instructions = instructions or existing_instructions

        md_content = f"""---
name: {final_name}
description: {final_desc or final_name}
version: "1.0.0"
metadata:
  openclaw:
    emoji: "🔧"
---

# Skill: {final_name}

{final_instructions}
"""
        skill_md.write_text(md_content, encoding="utf-8")

        changes = []
        if description:
            changes.append(f"descripción: {description}")
        if instructions:
            changes.append(f"instructions: {len(instructions)} characters")

        # Update skill.py if this is a Python skill and new code was provided
        skill_py = skill_dir / "skill.py"
        if code and skill_py.exists():
            # Safety scan on edited code, same gate as create
            _safe, _reason = _scan_python_code_safety(code)
            if not _safe:
                code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()[:16]
                logger.warning(
                    "skill_manager.python_edit_blocked",
                    slug=slug, name=final_name, reason=_reason, code_hash=code_hash,
                )
                asyncio.ensure_future(_audit_skill_manager(
                    action="edit_python", skill_name=final_name, success=False,
                    detail="edit rejected by safety scanner",
                    code_hash=code_hash, block_reason=_reason,
                ))
                return SkillResult(
                    skill_name="skill_manager", success=False, output="",
                    error=f"⛔ Edit blocked: {_reason}. Reformulate without sandbox-bypass constructs.",
                )
            try:
                existing_py = skill_py.read_text(encoding="utf-8")
                # Replace the execute function body while preserving class structure
                indented = "\n".join("        " + line for line in code.splitlines()) or "        pass"
                import re as _re
                new_py = _re.sub(
                    r"(async def execute\(self, \*\*kwargs\) -> SkillResult:\s*\n\s+_skill_name = [^\n]+\n\s+try:\n)(.+?)(        except Exception)",
                    lambda m: m.group(1) + indented + "\n" + m.group(3),
                    existing_py,
                    flags=_re.DOTALL,
                )
                if new_py != existing_py:
                    skill_py.write_text(new_py, encoding="utf-8")
                    # Re-load into registry
                    from ..openclaw.loader import load_python_skill
                    inst = load_python_skill(skill_dir)
                    if inst and self._registry:
                        self._registry.register(inst)
                    changes.append(f"Python code: {len(code)} characters")
                    logger.info("skill_manager.python_edited", slug=slug)
            except Exception as exc:
                logger.warning("skill_manager.python_edit_failed", slug=slug, error=str(exc))

        logger.info("skill_manager.edited", slug=slug, name=final_name)

        return SkillResult(
            skill_name="skill_manager",
            success=True,
            output=(
                f"Skill '{final_name}' edited.\n"
                f"Changes: {', '.join(changes) if changes else 'updated'}\n"
                f"Changes will be available in your next message."
            ),
        )

    # ── enable ───────────────────────────────────────────────────

    def _enable(self, name: str) -> SkillResult:
        if not name:
            return SkillResult(
                skill_name="skill_manager", success=False, output="",
                error="Name required.",
            )

        # Try built-in skill first
        if self._registry.enable(name):
            logger.info("skill_manager.enabled", skill=name, type="builtin")
            return SkillResult(
                skill_name="skill_manager", success=True,
                output=f"Skill '{name}' enabled.",
            )

        # Try custom skill (remove .disabled marker)
        slug = _slugify(name)
        skill_dir = get_skills_dir() / slug
        disabled_marker = skill_dir / ".disabled"

        if skill_dir.exists() and disabled_marker.exists():
            disabled_marker.unlink()
            logger.info("skill_manager.enabled", skill=slug, type="custom")
            return SkillResult(
                skill_name="skill_manager", success=True,
                output=f"Skill '{name}' enabled.",
            )

        # Also try exact directory name match
        for entry in get_skills_dir().iterdir():
            if entry.is_dir() and entry.name.lower() == name.lower():
                marker = entry / ".disabled"
                if marker.exists():
                    marker.unlink()
                    logger.info("skill_manager.enabled", skill=entry.name, type="custom")
                    return SkillResult(
                        skill_name="skill_manager", success=True,
                        output=f"Skill '{entry.name}' enabled.",
                    )

        return SkillResult(
            skill_name="skill_manager", success=False, output="",
            error=f"Skill '{name}' not found.",
        )

    # ── disable ──────────────────────────────────────────────────

    def _disable(self, name: str) -> SkillResult:
        if not name:
            return SkillResult(
                skill_name="skill_manager", success=False, output="",
                error="Name required.",
            )

        # Prevent disabling skill_manager itself
        if name.lower() in ("skill_manager", "skill-manager"):
            return SkillResult(
                skill_name="skill_manager", success=False, output="",
                error="You cannot disable the skill_manager itself.",
            )

        # Try built-in skill first
        if self._registry.disable(name):
            logger.info("skill_manager.disabled", skill=name, type="builtin")
            return SkillResult(
                skill_name="skill_manager", success=True,
                output=f"Skill '{name}' disabled.",
            )

        # Try custom skill (write .disabled marker)
        slug = _slugify(name)
        skill_dir = get_skills_dir() / slug
        if skill_dir.exists():
            (skill_dir / ".disabled").write_text("disabled", encoding="utf-8")
            logger.info("skill_manager.disabled", skill=slug, type="custom")
            return SkillResult(
                skill_name="skill_manager", success=True,
                output=f"Skill '{name}' disabled.",
            )

        # Try exact directory name match
        for entry in get_skills_dir().iterdir():
            if entry.is_dir() and entry.name.lower() == name.lower():
                (entry / ".disabled").write_text("disabled", encoding="utf-8")
                logger.info("skill_manager.disabled", skill=entry.name, type="custom")
                return SkillResult(
                    skill_name="skill_manager", success=True,
                    output=f"Skill '{entry.name}' disabled.",
                )

        return SkillResult(
            skill_name="skill_manager", success=False, output="",
            error=f"Skill '{name}' not found.",
        )

    # ── list ─────────────────────────────────────────────────────

    def _list(self) -> SkillResult:
        lines = []

        # Built-in skills
        all_defs = self._registry.list_all()
        builtin_lines = []
        for defn in sorted(all_defs, key=lambda d: d.name):
            enabled = self._registry.is_enabled(defn.name)
            status = "ON" if enabled else "OFF"
            builtin_lines.append(f"  [{status}] {defn.name} — {defn.description[:60]}")

        lines.append(f"Built-in skills ({len(all_defs)}):")
        lines.extend(builtin_lines)

        # Custom skills (OpenClaw / user-created)
        skills_dir = get_skills_dir()
        custom_lines = []
        custom_count = 0
        if skills_dir.exists():
            for entry in sorted(skills_dir.iterdir()):
                if not entry.is_dir():
                    continue
                skill_md = entry / "SKILL.md"
                if not skill_md.exists():
                    skill_md = entry / "skill.md"
                if not skill_md.exists():
                    continue
                custom_count += 1
                disabled = (entry / ".disabled").exists()
                status = "OFF" if disabled else "ON"
                # Try to read name from file
                try:
                    content = skill_md.read_text(encoding="utf-8")
                    name_match = re.search(r"^name:\s*(.+)", content, re.MULTILINE)
                    name = name_match.group(1).strip() if name_match else entry.name
                except Exception:
                    name = entry.name
                skill_type = "python" if (entry / "skill.py").exists() else "text"
                custom_lines.append(f"  [{status}] {name} ({skill_type}-custom: {entry.name}/)")

        if custom_lines:
            lines.append(f"\nCustom skills ({custom_count}):")
            lines.extend(custom_lines)
        else:
            lines.append("\nNo custom skills installed.")

        lines.append(
            '\nManage: skill_manager(action="enable|disable|create|delete", name="...")'
        )

        return SkillResult(
            skill_name="skill_manager", success=True,
            output="\n".join(lines),
        )

    # ── delete ───────────────────────────────────────────────────

    def _delete(self, name: str) -> SkillResult:
        if not name:
            return SkillResult(
                skill_name="skill_manager", success=False, output="",
                error="Name required.",
            )

        # Check if it's a built-in skill (can't delete those)
        all_names = {d.name for d in self._registry.list_all()}
        if name in all_names:
            return SkillResult(
                skill_name="skill_manager", success=False, output="",
                error=f"'{name}' is a built-in skill and cannot be deleted. Use action='disable' to deactivate it.",
            )

        # Find custom skill directory
        slug = _slugify(name)
        skill_dir = get_skills_dir() / slug

        if not skill_dir.exists():
            # Try exact name match
            for entry in get_skills_dir().iterdir():
                if entry.is_dir() and entry.name.lower() == name.lower():
                    skill_dir = entry
                    break

        if not skill_dir.exists():
            return SkillResult(
                skill_name="skill_manager", success=False, output="",
                error=f"Skill '{name}' not found in /data/skills/.",
            )

        shutil.rmtree(skill_dir)
        logger.info("skill_manager.deleted", slug=skill_dir.name)
        return SkillResult(
            skill_name="skill_manager", success=True,
            output=f"Skill '{name}' deleted.",
        )
