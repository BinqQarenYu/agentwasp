from src.skills.openclaw.loader import load_installed_skills

def test_repo_agents_skills_discovery():
    skills = load_installed_skills()
    slugs = [s.slug for s in skills]
    assert "lan-presence" in slugs
    skill = next(s for s in skills if s.slug == "lan-presence")
    assert skill.source == "crystallized"
    assert skill.name == "lan-presence"
