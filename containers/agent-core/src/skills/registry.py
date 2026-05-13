import json
from pathlib import Path

import structlog

from .base import SkillBase
from .types import SkillDefinition

logger = structlog.get_logger()

SKILLS_DIR = Path("/data/skills")


class SkillRegistry:
    """Central registry of all skills."""

    def __init__(self):
        self._skills: dict[str, SkillBase] = {}
        self._enabled: dict[str, bool] = {}
        # System defaults loaded from default_values.json (set by platform, not user)
        self._defaults: dict[str, dict] = {}
        # User edits loaded from builtin_override.json (set via dashboard)
        self._overrides: dict[str, dict] = {}

    def register(self, skill: SkillBase) -> None:
        defn = skill.definition()
        self._skills[defn.name] = skill
        self._enabled[defn.name] = defn.enabled
        logger.info("skill_registry.registered", skill=defn.name, category=defn.category)

    def get(self, name: str) -> SkillBase | None:
        if name not in self._skills or not self._enabled.get(name, False):
            return None
        return self._skills[name]

    def enable(self, name: str) -> bool:
        if name in self._skills:
            self._enabled[name] = True
            return True
        return False

    def disable(self, name: str) -> bool:
        if name in self._skills:
            self._enabled[name] = False
            return True
        return False

    def is_enabled(self, name: str) -> bool:
        return self._enabled.get(name, False)

    def apply_override(self, name: str, description: str | None = None, notes: str | None = None) -> None:
        """Apply a user override for a built-in skill's description/notes."""
        if name not in self._skills:
            return
        override = self._overrides.get(name, {})
        if description is not None:
            override["description"] = description
        if notes is not None:
            override["notes"] = notes
        self._overrides[name] = override

    def clear_override(self, name: str) -> None:
        """Remove user override, reverting to system defaults."""
        self._overrides.pop(name, None)

    def get_override(self, name: str) -> dict:
        """Return user-made overrides only (empty dict if none)."""
        return self._overrides.get(name, {})

    def get_effective(self, name: str) -> dict:
        """Return merged effective values: defaults layered with user overrides."""
        merged = dict(self._defaults.get(name, {}))
        merged.update(self._overrides.get(name, {}))
        return merged

    def load_overrides_from_disk(self) -> int:
        """Load default_values.json and builtin_override.json from /data/skills/."""
        if not SKILLS_DIR.exists():
            return 0
        defaults_loaded = 0
        overrides_loaded = 0
        for entry in SKILLS_DIR.iterdir():
            if not entry.is_dir():
                continue
            name = entry.name
            if name not in self._skills:
                continue
            # Layer 1: system defaults
            default_file = entry / "default_values.json"
            if default_file.exists():
                try:
                    self._defaults[name] = json.loads(default_file.read_text(encoding="utf-8"))
                    defaults_loaded += 1
                except Exception as exc:
                    logger.warning("skill_registry.default_load_failed", path=str(default_file), error=str(exc))
            # Layer 2: user overrides
            override_file = entry / "builtin_override.json"
            if override_file.exists():
                try:
                    self._overrides[name] = json.loads(override_file.read_text(encoding="utf-8"))
                    overrides_loaded += 1
                except Exception as exc:
                    logger.warning("skill_registry.override_load_failed", path=str(override_file), error=str(exc))

        logger.info("skill_registry.overrides_loaded", defaults=defaults_loaded, user_edits=overrides_loaded)
        return defaults_loaded + overrides_loaded

    def _apply_to_defn(self, defn: SkillDefinition) -> SkillDefinition:
        """Apply defaults then user overrides to a definition."""
        effective = self.get_effective(defn.name)
        if not effective:
            return defn
        updates = {}
        if effective.get("description"):
            updates["description"] = effective["description"]
        if updates:
            return defn.model_copy(update=updates)
        return defn

    def list_all(self) -> list[SkillDefinition]:
        return [self._apply_to_defn(s.definition()) for s in self._skills.values()]

    def list_enabled(self) -> list[SkillDefinition]:
        return [
            self._apply_to_defn(s.definition())
            for name, s in self._skills.items()
            if self._enabled.get(name, False)
        ]

    def format_for_prompt(self) -> str:
        enabled = self.list_enabled()
        if not enabled:
            return ""

        lines = ["Available skills you can use:"]
        for defn in enabled:
            params_desc = ", ".join(
                f'{p.name}: {p.param_type.value}'
                + (" (optional)" if not p.required else "")
                for p in defn.params
            )
            lines.append(f"- {defn.name}({params_desc}): {defn.description}")
            # Append effective notes (user override takes precedence over default)
            effective = self.get_effective(defn.name)
            notes = effective.get("notes", "")
            if notes:
                lines.append(f"  NOTE: {notes}")

        lines.append("")
        lines.append(
            'To use a skill, write: <skill>skill_name(param1="value1", param2="value2")</skill>'
        )
        lines.append("You can use multiple skills in one response. Wait for results before answering.")
        return "\n".join(lines)
