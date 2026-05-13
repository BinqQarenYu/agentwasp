"""Load OpenClaw skills from SKILL.md files on disk."""

from __future__ import annotations

import os
import re
from pathlib import Path

import structlog
import yaml

from .models import OpenClawMeta, OpenClawSkill

logger = structlog.get_logger()

SKILLS_DIR = "/data/skills"
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def get_skills_dir() -> Path:
    """Return the skills directory, creating it if needed."""
    p = Path(SKILLS_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _parse_skill_md(slug: str, content: str) -> OpenClawSkill | None:
    """Parse a SKILL.md file into an OpenClawSkill."""
    fm_match = _FRONTMATTER_RE.match(content)

    frontmatter: dict = {}
    body = content

    if fm_match:
        try:
            frontmatter = yaml.safe_load(fm_match.group(1)) or {}
        except yaml.YAMLError as e:
            logger.warning("openclaw.yaml_error", slug=slug, error=str(e))
            frontmatter = {}
        body = content[fm_match.end():]

    # Extract metadata.openclaw (or aliases)
    raw_meta = {}
    metadata = frontmatter.get("metadata", {})
    if isinstance(metadata, dict):
        raw_meta = (
            metadata.get("openclaw")
            or metadata.get("clawdbot")
            or metadata.get("clawdis")
            or {}
        )

    try:
        meta = OpenClawMeta.model_validate(raw_meta) if raw_meta else OpenClawMeta()
    except Exception:
        meta = OpenClawMeta()

    name = frontmatter.get("name", slug)
    description = frontmatter.get("description", "")
    version = str(frontmatter.get("version", "0.0.0"))

    # Trim body to reasonable size (max 3000 chars for prompt injection)
    instructions = body.strip()
    if len(instructions) > 3000:
        instructions = instructions[:3000] + "\n...(truncated)"

    return OpenClawSkill(
        slug=slug,
        name=name,
        description=description,
        version=version,
        meta=meta,
        instructions=instructions,
    )


def load_skill(skill_dir: Path) -> OpenClawSkill | None:
    """Load a single skill from its directory."""
    slug = skill_dir.name

    # Look for SKILL.md or skill.md
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        skill_md = skill_dir / "skill.md"
    if not skill_md.exists():
        return None

    try:
        content = skill_md.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("openclaw.read_error", slug=slug, error=str(e))
        return None

    return _parse_skill_md(slug, content)


def load_installed_skills() -> list[OpenClawSkill]:
    """Scan /data/skills/ and load all installed OpenClaw skills."""
    skills_dir = get_skills_dir()
    skills: list[OpenClawSkill] = []

    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir():
            continue
        # Skip disabled skills
        if (entry / ".disabled").exists():
            logger.debug("openclaw.skip_disabled", slug=entry.name)
            continue
        skill = load_skill(entry)
        if skill:
            skill.source = "clawhub" if (entry / ".clawhub").exists() else "local"
            skills.append(skill)
            logger.info("openclaw.loaded", slug=skill.slug, name=skill.name)

    return skills


def check_requirements(skill: OpenClawSkill) -> list[str]:
    """Check if a skill's requirements are met. Returns list of missing items."""
    missing: list[str] = []

    for bin_name in skill.meta.requires.bins:
        if not _bin_exists(bin_name):
            missing.append(f"binary: {bin_name}")

    for env_var in skill.meta.requires.env:
        if not os.environ.get(env_var):
            missing.append(f"env: {env_var}")

    if skill.meta.requires.any_bins:
        if not any(_bin_exists(b) for b in skill.meta.requires.any_bins):
            missing.append(f"any binary: {', '.join(skill.meta.requires.any_bins)}")

    return missing


def _bin_exists(name: str) -> bool:
    """Check if a binary exists on PATH."""
    import shutil
    return shutil.which(name) is not None


# ---------------------------------------------------------------------------
# Python skill loader
# ---------------------------------------------------------------------------

def load_python_skill(skill_dir: Path):
    """Load a SkillBase instance from skill_dir/skill.py.

    The file must define a class named ``Skill`` that extends SkillBase.
    Returns the Skill instance or None on error.
    """
    import importlib.util
    import sys

    skill_py = skill_dir / "skill.py"
    if not skill_py.exists():
        return None

    # Skip disabled skills
    if (skill_dir / ".disabled").exists():
        return None

    # Ensure /app is on sys.path so skill.py can do `from src.skills.base import ...`
    app_dir = "/app"
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)

    module_name = f"_dynamic_skill_{skill_dir.name}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, str(skill_py))
        if spec is None or spec.loader is None:
            logger.warning("python_skill.no_spec", slug=skill_dir.name)
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        skill_cls = getattr(module, "Skill", None)
        if skill_cls is None:
            logger.warning("python_skill.no_class", slug=skill_dir.name)
            return None
        instance = skill_cls()
        logger.info("python_skill.loaded", slug=skill_dir.name, name=instance.definition().name)
        return instance
    except Exception as exc:
        logger.warning("python_skill.load_error", slug=skill_dir.name, error=str(exc))
        return None


def load_all_python_skills() -> list:
    """Scan /data/skills/ and return SkillBase instances for every skill.py found."""
    skills_dir = get_skills_dir()
    instances = []
    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir():
            continue
        inst = load_python_skill(entry)
        if inst is not None:
            instances.append(inst)
    return instances
