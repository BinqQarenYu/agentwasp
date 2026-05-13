"""Skill management routes."""

import json
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

router = APIRouter()

SKILLS_DIR = Path("/data/skills")


def _slugify(name: str) -> str:
    """Convert a skill name to a filesystem-safe slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:50] or "custom-skill"


def _safe_skill_dir(slug: str) -> Path | None:
    """Return the skill dir only if slug is safe and inside SKILLS_DIR."""
    safe_slug = _slugify(slug)
    skill_dir = (SKILLS_DIR / safe_slug).resolve()
    try:
        skill_dir.relative_to(SKILLS_DIR.resolve())
    except ValueError:
        return None
    return skill_dir


def _load_custom_skills() -> list[dict]:
    """Load custom skills from /data/skills/ for dashboard display."""
    customs = []
    if not SKILLS_DIR.exists():
        return customs

    for entry in sorted(SKILLS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.exists():
            skill_md = entry / "skill.md"
        if not skill_md.exists():
            continue

        disabled = (entry / ".disabled").exists()

        # Parse basic info from SKILL.md
        try:
            content = skill_md.read_text(encoding="utf-8")
            name_match = re.search(r"^name:\s*(.+)", content, re.MULTILINE)
            desc_match = re.search(r"^description:\s*(.+)", content, re.MULTILINE)
            name = name_match.group(1).strip() if name_match else entry.name
            desc = desc_match.group(1).strip() if desc_match else ""
        except Exception:
            name = entry.name
            desc = ""

        customs.append({
            "name": name,
            "slug": entry.name,
            "description": desc,
            "category": "custom",
            "enabled": not disabled,
            "is_custom": True,
        })

    return customs


def _load_skill_content(slug: str) -> dict | None:
    """Load full skill content for editing."""
    skill_dir = SKILLS_DIR / slug
    if not skill_dir.exists():
        return None

    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        skill_md = skill_dir / "skill.md"
    if not skill_md.exists():
        return None

    try:
        content = skill_md.read_text(encoding="utf-8")
    except Exception:
        return None

    # Parse frontmatter
    name_match = re.search(r"^name:\s*(.+)", content, re.MULTILINE)
    desc_match = re.search(r"^description:\s*(.+)", content, re.MULTILINE)
    name = name_match.group(1).strip() if name_match else slug
    description = desc_match.group(1).strip() if desc_match else ""

    # Extract instructions (everything after the frontmatter and heading)
    instructions = ""
    # Remove frontmatter (between --- markers)
    body = re.sub(r"^---\s*\n.*?\n---\s*\n", "", content, count=1, flags=re.DOTALL)
    # Remove the "# Skill: ..." heading line
    body = re.sub(r"^#\s+Skill:.*\n*", "", body, count=1)
    instructions = body.strip()

    return {
        "name": name,
        "slug": slug,
        "description": description,
        "instructions": instructions,
    }


@router.get("/", response_class=HTMLResponse)
async def skills_page(request: Request):
    skill_registry = request.app.state.skill_registry
    skills = []
    if skill_registry:
        for defn in sorted(skill_registry.list_all(), key=lambda d: (d.category, d.name)):
            # is_overridden = user has a dashboard edit (builtin_override.json)
            user_override = skill_registry.get_override(defn.name)
            effective = skill_registry.get_effective(defn.name)
            skills.append({
                "name": defn.name,
                "slug": defn.name,
                "description": defn.description,
                "category": defn.category,
                "enabled": skill_registry.is_enabled(defn.name),
                "is_custom": False,
                "is_overridden": bool(user_override),
                # Values shown in edit modal (effective = defaults + user edits)
                "effective_description": effective.get("description", defn.description),
                "effective_notes": effective.get("notes", ""),
                "params": [p.name for p in defn.params] if defn.params else [],
                "capability_level": defn.capability_level or "controlled",
                "requires_confirmation": defn.requires_confirmation,
                "timeout_seconds": defn.timeout_seconds,
            })

    # Add custom skills from /data/skills/
    custom_skills = _load_custom_skills()
    skills.extend(custom_skills)

    return request.app.state.templates.TemplateResponse(request, "skills.html", {
        "skills": skills,
        "builtin_count": len(skills) - len(custom_skills),
        "custom_count": len(custom_skills),
    })


@router.post("/toggle")
async def toggle_skill(request: Request):
    form = await request.form()
    name = form.get("name", "")
    action = form.get("action", "")
    is_custom = form.get("is_custom", "") == "true"
    slug = form.get("slug", "")

    if is_custom and slug:
        skill_dir = _safe_skill_dir(slug)
        if skill_dir is None:
            return JSONResponse({"ok": False, "message": "Invalid skill slug"}, status_code=400)
        disabled_marker = skill_dir / ".disabled"
        if skill_dir.exists():
            if action == "disable":
                disabled_marker.write_text("disabled", encoding="utf-8")
            elif action == "enable" and disabled_marker.exists():
                disabled_marker.unlink()
    else:
        skill_registry = request.app.state.skill_registry
        if skill_registry and name:
            if action == "enable":
                skill_registry.enable(name)
            elif action == "disable":
                skill_registry.disable(name)

    label = name or slug
    if action == "enable":
        return JSONResponse({"ok": True, "message": f"{label} enabled", "action": "enable"})
    else:
        return JSONResponse({"ok": True, "message": f"{label} disabled", "action": "disable"})


@router.post("/create")
async def create_skill(request: Request):
    """Create a new custom skill from the dashboard form."""
    form = await request.form()
    name = form.get("name", "").strip()
    description = form.get("description", "").strip()
    instructions = form.get("instructions", "").strip()

    if not name or not instructions:
        return JSONResponse({"ok": False, "message": "Name and instructions required"}, status_code=400)

    slug = _slugify(name)
    skill_dir = SKILLS_DIR / slug
    skill_dir.mkdir(parents=True, exist_ok=True)

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

    # Remove .disabled if it exists
    disabled = skill_dir / ".disabled"
    if disabled.exists():
        disabled.unlink()

    return JSONResponse({"ok": True, "message": f"Skill '{name}' created"})


@router.get("/data/{slug}")
async def get_skill_data(request: Request, slug: str):
    """Return skill data as JSON for the edit modal."""
    skill = _load_skill_content(slug)
    if not skill:
        return JSONResponse({"ok": False, "message": "Skill not found"}, status_code=404)
    return JSONResponse({"ok": True, "skill": skill})


@router.post("/edit/{slug}")
async def save_skill(request: Request, slug: str):
    """Save changes to a custom skill."""
    form = await request.form()
    name = form.get("name", "").strip()
    description = form.get("description", "").strip()
    instructions = form.get("instructions", "").strip()

    skill_dir = _safe_skill_dir(slug)
    if skill_dir is None or not skill_dir.exists():
        return JSONResponse({"ok": False, "message": "Skill not found"}, status_code=404)

    md_content = f"""---
name: {name or slug}
description: {description or name or slug}
version: "1.0.0"
metadata:
  openclaw:
    emoji: "🔧"
---

# Skill: {name or slug}

{instructions}
"""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        skill_md = skill_dir / "skill.md"
    if not skill_md.exists():
        skill_md = skill_dir / "SKILL.md"

    skill_md.write_text(md_content, encoding="utf-8")

    return JSONResponse({"ok": True, "message": f"Skill '{name or slug}' saved"})


@router.post("/edit-builtin/{name}")
async def save_builtin_skill(request: Request, name: str):
    """Save description/notes override for a built-in skill."""
    skill_registry = request.app.state.skill_registry
    if not skill_registry:
        return JSONResponse({"ok": False, "message": "Skill registry unavailable"}, status_code=503)

    defn = next((d for d in skill_registry.list_all() if d.name == name), None)
    if not defn:
        return JSONResponse({"ok": False, "message": "Skill not found"}, status_code=404)

    form = await request.form()
    description = form.get("description", "").strip()
    notes = form.get("notes", "").strip()

    # Apply to live registry
    skill_registry.apply_override(name, description=description or None, notes=notes or None)

    # Persist to disk
    skill_dir = SKILLS_DIR / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    override_data = {}
    if description:
        override_data["description"] = description
    if notes:
        override_data["notes"] = notes
    override_file = skill_dir / "builtin_override.json"
    if override_data:
        override_file.write_text(json.dumps(override_data, ensure_ascii=False, indent=2), encoding="utf-8")
    elif override_file.exists():
        override_file.unlink()

    return JSONResponse({"ok": True, "message": f"Skill '{name}' updated"})


@router.post("/reset-builtin/{name}")
async def reset_builtin_skill(request: Request, name: str):
    """Remove all overrides for a built-in skill, restoring defaults."""
    skill_registry = request.app.state.skill_registry
    if not skill_registry:
        return JSONResponse({"ok": False, "message": "Skill registry unavailable"}, status_code=503)

    # Clear user override in memory
    skill_registry.clear_override(name)

    # Remove only the user override file — leave default_values.json intact
    override_file = SKILLS_DIR / name / "builtin_override.json"
    if override_file.exists():
        override_file.unlink()

    return JSONResponse({"ok": True, "message": f"'{name}' reset to defaults"})


@router.post("/delete")
async def delete_skill(request: Request):
    """Delete a custom skill."""
    form = await request.form()
    slug = form.get("slug", "")
    if slug:
        skill_dir = _safe_skill_dir(slug)
        if skill_dir and skill_dir.exists() and skill_dir.is_dir():
            shutil.rmtree(skill_dir)
            return JSONResponse({"ok": True, "message": f"{slug} deleted"})
    return JSONResponse({"ok": False, "message": "Skill not found"}, status_code=404)
