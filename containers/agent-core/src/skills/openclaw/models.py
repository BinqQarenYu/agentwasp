"""Pydantic models for the OpenClaw SKILL.md format."""

from __future__ import annotations

from pydantic import BaseModel, Field


class OpenClawRequirements(BaseModel):
    """Runtime requirements declared in metadata.openclaw.requires."""
    env: list[str] = Field(default_factory=list)
    bins: list[str] = Field(default_factory=list)
    any_bins: list[str] = Field(default_factory=list, alias="anyBins")
    config: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class OpenClawMeta(BaseModel):
    """The metadata.openclaw block from SKILL.md frontmatter."""
    requires: OpenClawRequirements = Field(default_factory=OpenClawRequirements)
    primary_env: str = Field(default="", alias="primaryEnv")
    emoji: str = ""
    always: bool = False
    skill_key: str = Field(default="", alias="skillKey")
    homepage: str = ""
    os: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class OpenClawSkill(BaseModel):
    """A parsed OpenClaw skill from a SKILL.md file."""
    slug: str  # directory name
    name: str  # from frontmatter or slug
    description: str = ""
    version: str = "0.0.0"
    meta: OpenClawMeta = Field(default_factory=OpenClawMeta)
    instructions: str = ""  # markdown body (the actual skill content)
    source: str = "local"  # "local" or "clawhub"

    @property
    def display_name(self) -> str:
        emoji = self.meta.emoji
        prefix = f"{emoji} " if emoji else ""
        return f"{prefix}{self.name}"

    @property
    def prompt_text(self) -> str:
        """Text to inject into the agent's system prompt."""
        header = f"## Skill: {self.display_name}"
        if self.description:
            header += f"\n{self.description}"
        return f"{header}\n\n{self.instructions}"
