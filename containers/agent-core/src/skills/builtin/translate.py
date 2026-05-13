import httpx

from ..base import SkillBase
from ..types import ParamType, SkillDefinition, SkillParam, SkillResult

# Language code mapping for common names
LANG_CODES = {
    "english": "en", "spanish": "es", "french": "fr", "german": "de",
    "italian": "it", "portuguese": "pt", "russian": "ru", "chinese": "zh",
    "japanese": "ja", "korean": "ko", "arabic": "ar", "dutch": "nl",
    "swedish": "sv", "norwegian": "no", "danish": "da", "finnish": "fi",
    "polish": "pl", "turkish": "tr", "hindi": "hi", "thai": "th",
    "ingles": "en", "espanol": "es", "frances": "fr", "aleman": "de",
}


class TranslateSkill(SkillBase):
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="translate",
            description="Translate text between languages using MyMemory API (free, no key).",
            params=[
                SkillParam(name="text", param_type=ParamType.STRING, description="Text to translate"),
                SkillParam(name="to", param_type=ParamType.STRING, description="Target language (e.g. 'en', 'es', 'french')"),
                SkillParam(name="from_lang", param_type=ParamType.STRING, description="Source language (auto-detect if empty)", required=False, default="auto"),
            ],
            category="utility",
            timeout_seconds=10.0,
            cooldown_seconds=1.0,
        )

    async def execute(self, text: str, to: str, from_lang: str = "auto", **kwargs) -> SkillResult:
        try:
            # Resolve language names to codes
            to_code = LANG_CODES.get(to.lower(), to.lower())
            from_code = LANG_CODES.get(from_lang.lower(), from_lang.lower()) if from_lang != "auto" else "auto"

            langpair = f"{from_code}|{to_code}" if from_code != "auto" else f"auto|{to_code}"

            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(
                    "https://api.mymemory.translated.net/get",
                    params={"q": text[:500], "langpair": langpair},
                )
                resp.raise_for_status()
                data = resp.json()

            translated = data.get("responseData", {}).get("translatedText", "")
            if not translated:
                return SkillResult(skill_name="translate", success=False, output="", error="Translation returned empty result")

            output = f"Translation ({langpair}):\n{translated}"
            return SkillResult(skill_name="translate", success=True, output=output)
        except Exception as e:
            return SkillResult(skill_name="translate", success=False, output="", error=str(e))
