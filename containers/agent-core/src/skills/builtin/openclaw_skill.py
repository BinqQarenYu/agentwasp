"""OpenClaw skill management — search, install, list, remove ClawHub skills."""

from __future__ import annotations

import shutil

from ..base import SkillBase
from ..types import SkillDefinition, SkillParam, SkillResult, ParamType
from ..openclaw.clawhub_client import get_client
from ..openclaw.loader import get_skills_dir, load_installed_skills, load_skill, check_requirements


class OpenClawSkill(SkillBase):
    """Manage OpenClaw-compatible skills from ClawHub."""

    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="openclaw",
            description="Search, install, list, or remove OpenClaw skills from ClawHub",
            params=[
                SkillParam(
                    name="action",
                    description="Action: search, install, list, remove, info",
                    required=True,
                ),
                SkillParam(
                    name="query",
                    description="Search query (for action=search)",
                    required=False,
                    default="",
                ),
                SkillParam(
                    name="slug",
                    description="Skill slug (for action=install/remove/info)",
                    required=False,
                    default="",
                ),
            ],
            category="system",
            timeout_seconds=60.0,
        )

    async def execute(self, **kwargs) -> SkillResult:
        action = kwargs.get("action", "list")
        query = kwargs.get("query", "")
        slug = kwargs.get("slug", "")

        try:
            if action == "search":
                return await self._search(query)
            elif action == "install":
                return await self._install(slug)
            elif action == "list":
                return self._list()
            elif action == "remove":
                return self._remove(slug)
            elif action == "info":
                return self._info(slug)
            else:
                return SkillResult(
                    skill_name="openclaw",
                    success=False,
                    output="",
                    error=f"Unknown action: {action}. Use: search, install, list, remove, info",
                )
        except Exception as e:
            return SkillResult(
                skill_name="openclaw",
                success=False,
                output="",
                error=str(e),
            )

    async def _search(self, query: str) -> SkillResult:
        if not query:
            return SkillResult(
                skill_name="openclaw", success=False, output="",
                error="Query required for search. Use: openclaw(action=\"search\", query=\"...\")",
            )

        client = get_client()
        results = await client.search(query, limit=8)

        if not results:
            return SkillResult(
                skill_name="openclaw", success=True,
                output=f"No skills found for: {query}",
            )

        lines = [f"ClawHub results for \"{query}\":\n"]
        for r in results:
            stars = f" ({r['stars']}*)" if r.get("stars") else ""
            ver = f" v{r['version']}" if r.get("version") else ""
            lines.append(f"  {r['slug']}{ver}{stars}")
            if r.get("description"):
                lines.append(f"    {r['description'][:100]}")
        lines.append(f"\nInstall: openclaw(action=\"install\", slug=\"<name>\")")

        return SkillResult(
            skill_name="openclaw", success=True,
            output="\n".join(lines),
        )

    async def _install(self, slug: str) -> SkillResult:
        if not slug:
            return SkillResult(
                skill_name="openclaw", success=False, output="",
                error="Slug required. Use: openclaw(action=\"install\", slug=\"skill-name\")",
            )

        # Check if already installed
        skill_dir = get_skills_dir() / slug
        if skill_dir.exists():
            return SkillResult(
                skill_name="openclaw", success=False, output="",
                error=f"Skill '{slug}' is already installed. Remove first to reinstall.",
            )

        client = get_client()
        skill = await client.download(slug)

        if not skill:
            return SkillResult(
                skill_name="openclaw", success=False, output="",
                error=f"Failed to download skill '{slug}'. Check the slug and try again.",
            )

        # Check requirements
        missing = check_requirements(skill)
        warning = ""
        if missing:
            warning = f"\nWarning — missing requirements: {', '.join(missing)}"

        return SkillResult(
            skill_name="openclaw", success=True,
            output=(
                f"Installed: {skill.display_name} (v{skill.version})\n"
                f"{skill.description}\n"
                f"Instructions loaded ({len(skill.instructions)} chars)"
                f"{warning}"
            ),
        )

    def _list(self) -> SkillResult:
        skills = load_installed_skills()

        if not skills:
            return SkillResult(
                skill_name="openclaw", success=True,
                output="No OpenClaw skills installed.\nSearch: openclaw(action=\"search\", query=\"...\")",
            )

        lines = [f"Installed OpenClaw skills ({len(skills)}):\n"]
        for s in skills:
            src = "[clawhub]" if s.source == "clawhub" else "[local]"
            lines.append(f"  {s.display_name} v{s.version} {src}")
            if s.description:
                lines.append(f"    {s.description[:80]}")
            missing = check_requirements(s)
            if missing:
                lines.append(f"    Missing: {', '.join(missing)}")

        return SkillResult(
            skill_name="openclaw", success=True,
            output="\n".join(lines),
        )

    def _remove(self, slug: str) -> SkillResult:
        if not slug:
            return SkillResult(
                skill_name="openclaw", success=False, output="",
                error="Slug required. Use: openclaw(action=\"remove\", slug=\"skill-name\")",
            )

        skill_dir = get_skills_dir() / slug
        if not skill_dir.exists():
            return SkillResult(
                skill_name="openclaw", success=False, output="",
                error=f"Skill '{slug}' is not installed.",
            )

        # Load name before removing
        skill = load_skill(skill_dir)
        name = skill.display_name if skill else slug

        shutil.rmtree(skill_dir)
        return SkillResult(
            skill_name="openclaw", success=True,
            output=f"Removed: {name}",
        )

    def _info(self, slug: str) -> SkillResult:
        if not slug:
            return SkillResult(
                skill_name="openclaw", success=False, output="",
                error="Slug required.",
            )

        skill_dir = get_skills_dir() / slug
        if not skill_dir.exists():
            return SkillResult(
                skill_name="openclaw", success=False, output="",
                error=f"Skill '{slug}' is not installed.",
            )

        skill = load_skill(skill_dir)
        if not skill:
            return SkillResult(
                skill_name="openclaw", success=False, output="",
                error=f"Could not parse SKILL.md for '{slug}'.",
            )

        lines = [
            f"Name: {skill.display_name}",
            f"Version: {skill.version}",
            f"Description: {skill.description}",
            f"Source: {skill.source}",
        ]
        if skill.meta.requires.bins:
            lines.append(f"Requires bins: {', '.join(skill.meta.requires.bins)}")
        if skill.meta.requires.env:
            lines.append(f"Requires env: {', '.join(skill.meta.requires.env)}")
        if skill.meta.homepage:
            lines.append(f"Homepage: {skill.meta.homepage}")

        missing = check_requirements(skill)
        if missing:
            lines.append(f"Missing: {', '.join(missing)}")

        lines.append(f"\nInstructions ({len(skill.instructions)} chars):")
        preview = skill.instructions[:500]
        lines.append(preview)

        return SkillResult(
            skill_name="openclaw", success=True,
            output="\n".join(lines),
        )
