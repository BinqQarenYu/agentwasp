import pytest
from src.utils.lang_detect import detect_lang

# ── Edge Cases & Defaults ─────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "",
    "   ",
    "\n\t",
    None,
])
def test_detect_lang_empty_or_whitespace_defaults_to_en(text):
    """Empty, whitespace-only, or None input should default to English."""
    assert detect_lang(text) == "en"

# ── Non-Latin Scripts ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, expected_lang", [
    # Chinese (requires >= 2 CJK characters, no Japanese kana)
    ("你好", "zh"),
    ("这是一个中文句子", "zh"),
    ("中", "en"), # Only 1 char, falls back to EN

    # Japanese (requires Hiragana/Katakana presence)
    ("こんにちは", "ja"), # Only kana
    ("私は日本語を話します", "ja"), # CJK + kana (must return 'ja' not 'zh')
    ("コンピューター", "ja"), # Katakana
    ("あ", "ja"), # Only 1 char, but Japanese regex doesn't require >= 2

    # Korean (requires >= 2 Hangul syllables)
    ("안녕하세요", "ko"),
    ("한국어", "ko"),
    ("한", "en"), # Only 1 char, falls back to EN

    # Arabic (requires >= 2 Arabic characters)
    ("مرحبا", "ar"),
    ("كيف حالك", "ar"),
    ("م", "en"), # Only 1 char, falls back to EN

    # Russian/Cyrillic (requires >= 3 Cyrillic characters)
    ("привет", "ru"),
    ("как дела", "ru"),
    ("пр", "en"), # Only 2 chars, falls back to EN
])
def test_detect_lang_non_latin(text, expected_lang):
    """Test character density detection for non-Latin scripts."""
    assert detect_lang(text) == expected_lang

# ── Latin Scripts ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, expected_lang", [
    # Spanish
    ("¿cómo estás?", "es"),
    ("necesito ayuda por favor", "es"),
    ("tengo una pregunta", "es"),

    # Portuguese (adjusting test to use words clearly distinct from ES since 'como' and 'está' overlap and tie breaks to ES)
    ("olá, você pode me ajudar?", "pt"),
    ("preciso de ajuda por favor", "pt"),
    ("tenho uma pergunta", "pt"),
    ("bom dia, tudo bem?", "pt"),

    # French
    ("comment allez-vous?", "fr"),
    ("je cherche quelque chose", "fr"),
    ("s'il vous plaît", "fr"),

    # German
    ("wie geht es dir?", "de"),
    ("ich brauche hilfe", "de"),
    ("guten morgen", "de"),

    # English
    ("how do you do?", "en"),
    ("i need some help", "en"),
    ("could you please fetch that?", "en"),

    # No matches, should default to English
    ("xyzzy bleep bloop", "en"),
    ("12345 67890", "en"),
])
def test_detect_lang_latin(text, expected_lang):
    """Test function word heuristics for Latin scripts."""
    assert detect_lang(text) == expected_lang

# ── Edge Cases in Latin Scoring ───────────────────────────────────────────────

def test_detect_lang_tie_breaker_prefers_en():
    """If there's a tie that includes English, English should win."""
    # "hola" (es: 1) + "hello" (en: 1) -> tie between es and en
    # Our heuristic says: if scores[best] == scores["en"] and best != "en": return "en"
    text = "hola hello"
    assert detect_lang(text) == "en"

def test_detect_lang_tie_breaker_non_english():
    """If there's a tie not involving English, the first one max() hits wins.
    In Python >3.7 dict order is preserved.
    Dict order: 'es', 'pt', 'fr', 'de', 'en'.
    If 'es' and 'pt' tie with 1, 'es' wins."""
    # "como" is in 'es' and 'pt' and 'pt' is 1, 'es' is 1. 'es' is first.
    text = "como"
    assert detect_lang(text) == "es"
