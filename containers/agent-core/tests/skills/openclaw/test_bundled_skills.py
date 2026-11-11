import os
from pathlib import Path

os.environ.setdefault("MEDIA_SIGNING_SECRET", "test-media-signing-secret-for-pytest")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://agent:agent@localhost/agent")

from src.skills.openclaw import loader
from src.skills.openclaw.models import OpenClawSkill


def _write_skill(parent: Path, slug: str, name: str) -> Path:
    skill_dir = parent / slug
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test\n---\n\nInstructions for {name}.",
        encoding="utf-8",
    )
    return skill_dir


def test_loads_bundled_prompt_skills_alongside_user_skills(tmp_path, monkeypatch):
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    _write_skill(user_dir, "custom-workflow", "Custom workflow")
    monkeypatch.setattr(loader, "SKILLS_DIR", user_dir)

    skills = loader.load_installed_skills()

    assert [(skill.slug, skill.source) for skill in skills] == [
        ("engineering-tdd", "builtin"),
        ("custom-workflow", "local"),
    ]
    assert "confirm it fails for the expected reason" in skills[0].instructions


def test_user_skill_overrides_bundled_skill_with_same_slug(tmp_path, monkeypatch):
    bundled_dir = tmp_path / "bundled"
    user_dir = tmp_path / "user"
    bundled_dir.mkdir()
    user_dir.mkdir()
    _write_skill(bundled_dir, "engineering-tdd", "Bundled TDD")
    _write_skill(user_dir, "engineering-tdd", "Custom TDD")
    monkeypatch.setattr(loader, "BUNDLED_SKILLS_DIR", bundled_dir)
    monkeypatch.setattr(loader, "SKILLS_DIR", user_dir)

    skills = loader.load_installed_skills()

    assert len(skills) == 1
    assert skills[0].name == "Custom TDD"
    assert skills[0].source == "local"


def test_disabled_user_skill_suppresses_bundled_skill(tmp_path, monkeypatch):
    bundled_dir = tmp_path / "bundled"
    user_dir = tmp_path / "user"
    bundled_dir.mkdir()
    user_dir.mkdir()
    _write_skill(bundled_dir, "engineering-tdd", "Test-driven development")
    (user_dir / "engineering-tdd").mkdir()
    (user_dir / "engineering-tdd" / ".disabled").touch()
    monkeypatch.setattr(loader, "BUNDLED_SKILLS_DIR", bundled_dir)
    monkeypatch.setattr(loader, "SKILLS_DIR", user_dir)

    assert loader.load_installed_skills() == []


def test_bundled_skill_source_has_a_distinct_label():
    skill = OpenClawSkill(
        slug="engineering-tdd",
        name="Test-driven development",
        description="TDD guidance",
        source="builtin",
    )

    assert skill.source_label == "bundled"
