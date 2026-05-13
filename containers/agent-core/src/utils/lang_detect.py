"""Simple language detector for user messages.

Strategy:
- Unicode ranges for non-Latin scripts (reliable, zero false positives)
- Function-word heuristics for Latin-script languages
- Defaults to "en" when ambiguous

Returns ISO 639-1 codes: "en", "es", "pt", "fr", "de", "zh", "ja", "ko", "ar"
"""

from __future__ import annotations

import re

# ── Non-Latin script ranges (Unicode — very reliable) ─────────────────────────

_ZH_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")          # CJK unified + extension A
_JA_RE = re.compile(r"[\u3040-\u309f\u30a0-\u30bf]")          # Hiragana + Katakana
_KO_RE = re.compile(r"[\uac00-\ud7a3]")                        # Hangul syllables
_AR_RE = re.compile(r"[\u0600-\u06ff]")                        # Arabic block
_RU_RE = re.compile(r"[\u0400-\u04ff]")                        # Cyrillic (covers Russian/Ukrainian)

# ── Latin-script function words ────────────────────────────────────────────────

_ES = re.compile(
    r"\b(?:que|qué|cómo|como|cuándo|cuando|dónde|donde|también|además|pero|"
    r"tengo|tienes?|tiene[ns]?|necesito|quiero|puedo|puedes?|puede[ns]?|"
    r"haciendo|captura(?:r)?|envía(?:me)?|dime|dame|hola|gracias|"
    r"por\s+favor|buenas?|buenos?\s+d[ií]as?|está(?:s|n)?|estoy|"
    r"de\s+la\s+\w|el\s+\w|la\s+\w|los\s+\w|las\s+\w|"
    r"un[ao]?\s+\w|me\s+(?:puedes?|dices?|envías?|das?))\b",
    re.IGNORECASE,
)

_PT = re.compile(
    r"\b(?:que|como|quando|onde|também|além|mas|tenho|tens|tem|"
    r"preciso|quero|posso|podes?|pode[ms]?|fazendo|capturar|"
    r"olá|obrigad[oa]|por\s+favor|bom\s+dia|está(?:s|n|va|mos)?|"
    r"estou|de\s+la?\s+\w|[oa][s]?\s+\w|um[a]?\s+\w|"
    r"me\s+(?:podes?|dizes?|envias?|dás?))\b",
    re.IGNORECASE,
)

_FR = re.compile(
    r"\b(?:comment|quand|pourquoi|parce\s+que|aussi|mais|cependant|"
    r"je\s+(?:veux|peux|suis|voudrais|cherche|peux)|vous|nous|ils|elles|"
    r"s'il\s+vous\s+plaît|merci|bonjour|bonsoir|est-ce|n'est|"
    r"le[s]?\s+\w|la\s+\w|les\s+\w|un[e]?\s+\w|des\s+\w|du\s+\w)\b",
    re.IGNORECASE,
)

_DE = re.compile(
    r"\b(?:wie|wann|warum|weil|auch|aber|obwohl|dann|ich\s+(?:will|kann|möchte|brauche|suche)|"
    r"bitte|danke|hallo|guten\s+(?:morgen|tag|abend)|"
    r"der\s+\w|die\s+\w|das\s+\w|ein[e]?\s+\w|"
    r"machen|ist\s+\w|sind\s+\w|haben|habe|werden)\b",
    re.IGNORECASE,
)

_EN = re.compile(
    r"\b(?:the\s+\w|this\s+\w|that\s+\w|these\s+\w|those\s+\w|"
    r"i\s+(?:want|need|can|would|am|have|like)|"
    r"can\s+you|could\s+you|please|thank|hello|hi\b|hey\b|"
    r"how\s+(?:do|can|does|to)\s+\w|what\s+(?:is|are|was)\s+\w|"
    r"will\s+you|do\s+you|are\s+you|have\s+you|"
    r"screenshot|capture|scroll|navigate|fetch|search\s+for)\b",
    re.IGNORECASE,
)


def detect_lang(text: str) -> str:
    """Return ISO 639-1 language code for the given text. Defaults to 'en'."""
    if not text or not text.strip():
        return "en"

    # Non-Latin scripts — check character density (at least 2 chars to avoid single loanwords)
    if len(_ZH_RE.findall(text)) >= 2:
        # Distinguish Japanese (has kana) from Chinese
        if _JA_RE.search(text):
            return "ja"
        return "zh"
    if _JA_RE.search(text):
        return "ja"
    if len(_KO_RE.findall(text)) >= 2:
        return "ko"
    if len(_AR_RE.findall(text)) >= 2:
        return "ar"
    if len(_RU_RE.findall(text)) >= 3:
        return "ru"

    # Latin-script languages — score by function word matches
    scores: dict[str, int] = {
        "es": len(_ES.findall(text)),
        "pt": len(_PT.findall(text)),
        "fr": len(_FR.findall(text)),
        "de": len(_DE.findall(text)),
        "en": len(_EN.findall(text)),
    }

    best = max(scores, key=lambda k: scores[k])
    if scores[best] == 0:
        return "en"
    # Tie → prefer English
    if scores[best] == scores["en"] and best != "en":
        return "en"
    return best
