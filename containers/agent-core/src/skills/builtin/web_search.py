import asyncio

from ddgs import DDGS

from ..base import SkillBase
from ..types import ParamType, SkillDefinition, SkillParam, SkillResult


def _do_search(query: str, max_n: int, region: str) -> list:
    """Run web search synchronously (called via asyncio.to_thread)."""
    with DDGS() as ddgs:
        return list(ddgs.text(query, max_results=max_n, region=region))


class WebSearchSkill(SkillBase):
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="web_search",
            description="Search the web.",
            params=[
                SkillParam(name="query", param_type=ParamType.STRING, description="Search query"),
                SkillParam(
                    name="max_results",
                    param_type=ParamType.INTEGER,
                    description="Max results (1-10)",
                    required=False,
                    default="5",
                ),
                SkillParam(
                    name="lang",
                    param_type=ParamType.STRING,
                    description="Language/region code (e.g. 'es-es', 'en-us')",
                    required=False,
                    default="es-es",
                ),
            ],
            category="web",
            timeout_seconds=30.0,
            cooldown_seconds=1.0,
        )

    async def execute(self, query: str, max_results: str = "5", lang: str = "es-es", **kwargs) -> SkillResult:
        max_n = min(int(max_results), 10)
        region_map = {"es": "es-es", "en": "en-us", "pt": "pt-br", "fr": "fr-fr", "de": "de-de"}
        region = region_map.get(lang, lang)
        try:
            results = await asyncio.to_thread(_do_search, query, max_n, region)

            if not results:
                return SkillResult(skill_name="web_search", success=True, output=f"No results for: {query}")

            lines = []
            for r in results:
                title = r.get("title", "")
                url = r.get("href", "")
                desc = r.get("body", "")
                if title:
                    lines.append(f"- {title}: {url}")
                    if desc:
                        lines.append(f"  {desc[:150]}")
                else:
                    lines.append(f"- {url}")

            output = f"Search results for '{query}':\n" + "\n".join(lines)
            return SkillResult(skill_name="web_search", success=True, output=output)
        except Exception as e:
            return SkillResult(skill_name="web_search", success=False, output="", error=str(e))
