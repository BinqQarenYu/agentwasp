"""English-canonical → user-language translator with Redis cache.

The agent's internal voice is English. This module translates canonical
system messages (refusals, confirmations, fallbacks) into the user's
detected language at publish time. Translations are cached so repeated
phrases hit Redis instead of the LLM.

Design
------
- Single LLM call per (text, target_lang) pair, then cached for 30 days.
- Cache key: ``i18n:{target_lang}:{sha256(text)[:16]}``
- Fail-safe: any error falls back to the original English text — truth
  enforcement never breaks because translation failed.
- Preserves placeholders, URLs, IPs, emojis, numbers via prompt instruction.

Public API
----------
- ``translate(text, target_lang, model_manager, redis_url)`` — async; returns
  translated text or original on any failure.
- ``apick(intent, lang, seed, model_manager, redis_url, **kwargs)`` — async
  one-shot: pulls canonical English from phrases.py and translates it.
"""
from __future__ import annotations

import asyncio
import hashlib

import structlog

from ..models.types import Message, ModelRequest

logger = structlog.get_logger()

_CACHE_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days
_TRANSLATE_TIMEOUT_S = 8.0
_TRANSLATE_MAX_TOKENS = 250
_TRANSLATE_TEMP = 0.0  # deterministic — same input always → same translation

# ── Telemetry counters (Redis hashes, daily-bucketed) ─────────────────────────
# Keys:
#   i18n:metrics:total       — single hash, all-time counters
#   i18n:metrics:day:YYYY-MM-DD — daily counters (TTL 60d)
#
# Fields per hash:
#   cache_hits, cache_misses, llm_calls, llm_failures, latency_ms_sum, latency_count
#
# Read via dashboard route /metrics/i18n.
_METRICS_TOTAL_KEY = "i18n:metrics:total"
_METRICS_DAY_TTL = 60 * 60 * 24 * 60  # 60 days


async def _bump_metric(redis_url: str | None, field: str, by: int = 1) -> None:
    """Best-effort metric increment. Never raises — telemetry failure must
    not break a publish."""
    if not redis_url:
        return
    try:
        import datetime as _dt
        import redis.asyncio as aioredis
        day_key = f"i18n:metrics:day:{_dt.date.today().isoformat()}"
        r = aioredis.from_url(redis_url, decode_responses=True)
        try:
            pipe = r.pipeline()
            pipe.hincrby(_METRICS_TOTAL_KEY, field, by)
            pipe.hincrby(day_key, field, by)
            pipe.expire(day_key, _METRICS_DAY_TTL)
            await pipe.execute()
        finally:
            await r.aclose()
    except Exception:
        pass


_TRANSLATOR_PROMPT = """\
Translate the message below to {target_lang_label}. Hard rules:
1. Preserve URLs, IP addresses, email addresses, file paths, hostnames, and proper nouns EXACTLY (do not translate them).
2. Preserve emojis (e.g. ✅, ⛔, 🤖) exactly.
3. Preserve numbers, dates, clock times, percentages, currency amounts verbatim.
4. Do NOT add quotes, preamble, "Translation:", explanation, or any wrapping. Output ONLY the translated text on a single response.
5. Match the message's register (informal vs formal) and length. Keep it natural, do not literal-translate idioms.

Message:
{text}
"""

# ISO 639-1 → human label for the prompt.  Languages not listed pass through
# the raw code so the LLM still gets a useful hint (it understands "ja",
# "ko", etc. directly).
_LANG_LABELS = {
    "es":   "Spanish (neutral, no voseo, use 'tú')",
    "es-ar": "Spanish (Argentina, voseo)",
    "es-mx": "Spanish (Mexico)",
    "es-cl": "Spanish (Chile, neutral)",
    "en":   "English",
    "pt":   "Portuguese",
    "pt-br": "Portuguese (Brazil)",
    "fr":   "French",
    "de":   "German",
    "it":   "Italian",
    "ja":   "Japanese",
    "ko":   "Korean",
    "zh":   "Chinese (Simplified)",
    "zh-cn": "Chinese (Simplified)",
    "zh-tw": "Chinese (Traditional)",
    "ru":   "Russian",
    "ar":   "Arabic",
    "he":   "Hebrew",
    "nl":   "Dutch",
    "sv":   "Swedish",
    "no":   "Norwegian",
    "da":   "Danish",
    "fi":   "Finnish",
    "pl":   "Polish",
    "tr":   "Turkish",
    "cs":   "Czech",
    "ro":   "Romanian",
    "hu":   "Hungarian",
    "uk":   "Ukrainian",
    "vi":   "Vietnamese",
    "th":   "Thai",
    "id":   "Indonesian",
    "hi":   "Hindi",
    "el":   "Greek",
    "ca":   "Catalan",
    "eu":   "Basque",
}


def _normalise_lang(lang: str | None) -> str:
    """Normalise common variants. Returns "" for English-equivalent so callers
    can short-circuit without a translation call."""
    if not lang:
        return ""
    lang = lang.strip().lower()
    if lang in ("en", "en-us", "en-gb", "english", "eng"):
        return ""
    return lang


def _cache_key(target_lang: str, text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"i18n:{target_lang}:{digest}"


async def translate(
    text: str,
    target_lang: str,
    model_manager,
    redis_url: str | None = None,
) -> str:
    """Translate ``text`` (English) to ``target_lang``.

    Returns the translated text. Any failure path returns the original
    English so user-visible behaviour is "correct content, possibly wrong
    language" rather than a broken publish.
    """
    if not text:
        return text or ""
    norm_lang = _normalise_lang(target_lang)
    if not norm_lang:
        return text  # English or empty target — nothing to do

    # ── Cache lookup ──────────────────────────────────────────────────────
    if redis_url:
        try:
            import redis.asyncio as aioredis
            r = aioredis.from_url(redis_url, decode_responses=True)
            try:
                cached = await r.get(_cache_key(norm_lang, text))
                if cached:
                    await _bump_metric(redis_url, "cache_hits")
                    return cached
            finally:
                await r.aclose()
        except Exception as e:
            logger.debug("translator.cache_read_failed", error=str(e)[:80])

    await _bump_metric(redis_url, "cache_misses")

    # ── LLM translation ───────────────────────────────────────────────────
    if model_manager is None:
        await _bump_metric(redis_url, "llm_failures")
        return text  # fail-safe — can't translate without a model

    label = _LANG_LABELS.get(norm_lang, norm_lang)
    prompt = _TRANSLATOR_PROMPT.format(target_lang_label=label, text=text)
    import time as _time
    _t0 = _time.monotonic()
    try:
        request = ModelRequest(
            messages=[Message(role="user", content=prompt)],
            temperature=_TRANSLATE_TEMP,
            max_tokens=_TRANSLATE_MAX_TOKENS,
        )
        response = await asyncio.wait_for(
            model_manager.generate(request),
            timeout=_TRANSLATE_TIMEOUT_S,
        )
        translated = (response.content or "").strip()
        await _bump_metric(redis_url, "llm_calls")
        _latency_ms = int((_time.monotonic() - _t0) * 1000)
        await _bump_metric(redis_url, "latency_ms_sum", _latency_ms)
        await _bump_metric(redis_url, "latency_count")
    except Exception as e:
        logger.warning(
            "translator.llm_failed",
            target_lang=norm_lang,
            error=str(e)[:120],
        )
        await _bump_metric(redis_url, "llm_failures")
        return text

    # Strip common LLM wrappers ("Translation:", quotes, code fences)
    translated = translated.strip().strip("`").strip()
    if translated.lower().startswith(("translation:", "translated:", "output:")):
        translated = translated.split(":", 1)[1].strip()
    if translated.startswith(('"', "'", "“", "‘")) and translated.endswith(('"', "'", "”", "’")):
        translated = translated[1:-1].strip()
    if not translated:
        return text

    # ── Cache write ───────────────────────────────────────────────────────
    if redis_url:
        try:
            import redis.asyncio as aioredis
            r = aioredis.from_url(redis_url, decode_responses=True)
            try:
                await r.set(
                    _cache_key(norm_lang, text),
                    translated,
                    ex=_CACHE_TTL_SECONDS,
                )
            finally:
                await r.aclose()
        except Exception as e:
            logger.debug("translator.cache_write_failed", error=str(e)[:80])

    logger.info(
        "translator.translated",
        target_lang=norm_lang,
        src_chars=len(text),
        out_chars=len(translated),
    )
    return translated


async def apick(
    intent: str,
    lang: str,
    seed: str,
    model_manager,
    redis_url: str | None = None,
    /,
    **kwargs,
) -> str:
    """Async pick: returns canonical English template translated to ``lang``.

    Convenience wrapper combining ``phrases.pick`` (sync, returns canonical
    English) with ``translate``. ``model_manager`` and ``redis_url`` are
    required to actually translate; if either is missing, the canonical
    English is returned as fail-safe.
    """
    from .phrases import pick as _pick_canonical
    canonical = _pick_canonical(intent, seed=seed, **kwargs)
    norm = _normalise_lang(lang)
    if not norm or model_manager is None:
        return canonical
    return await translate(canonical, norm, model_manager, redis_url)


# ── Heuristic language detector for the final guard ──────────────────────────
#
# Catches the case where an LLM-generated response leaks into the user's
# session in a different language (e.g. an English refusal sent to a
# Spanish chat after multiple replan attempts). Lightweight, no external
# dependency, conservative — only flags when the signal is strong.

import re as _re_lang


_ES_MARKERS = _re_lang.compile(
    r"(?:¿|¡|ñ|á|é|í|ó|ú|"
    r"\b(?:el|la|los|las|que|es|en|un|una|del|para|con|por|sobre|según|sin|"
    r"pero|porque|cómo|cuál|cuándo|dónde|qué|sí|no|yo|tú|usted|ustedes|"
    r"haz|hago|haces|hace|hacer|tienes|tengo|tiene|hay|este|esta|esto|"
    r"está|están|estás|estoy|estaba|estaban|fue|fueron|seré|sería|"
    r"acá|aquí|allá|hola|chao|gracias|disculpa|perdón)\b)",
    _re_lang.IGNORECASE,
)
_EN_MARKERS = _re_lang.compile(
    r"\b(?:the|a|an|is|was|were|are|am|i|you|we|they|he|she|it|"
    r"want|try|can|cannot|can't|won't|don't|doesn't|didn't|will|would|should|"
    r"could|need|have|has|had|did|do|does|been|being|"
    r"and|or|but|because|when|where|why|how|which|that|this|those|these|"
    r"hello|hi|thanks|sorry|please|here|there|now|then|"
    r"send|got|get|getting|sent|going|made|make|making|"
    r"with|for|of|from|about|into|out|over|under|through)\b",
    _re_lang.IGNORECASE,
)
_PT_MARKERS = _re_lang.compile(
    r"\b(?:nao|não|você|voce|fazer|fiz|fazendo|estou|esta|estão|"
    r"obrigado|por\s+favor|olá|ola|tudo\s+bem)\b",
    _re_lang.IGNORECASE,
)
_FR_MARKERS = _re_lang.compile(
    r"\b(?:je|tu|vous|nous|ils|elle|ne|pas|"
    r"bonjour|merci|s'il\s+vous\s+plaît|comment|pourquoi|"
    r"avec|sans|pour|sur|dans|peux|pouvez|veux|vouloir|fait|faire)\b",
    _re_lang.IGNORECASE,
)
_DE_MARKERS = _re_lang.compile(
    r"\b(?:ich|du|sie|wir|nicht|ist|sind|war|waren|"
    r"hallo|danke|bitte|möchte|möchten|kann|können|machen|gemacht|"
    r"die|der|das|den|des|ein|eine|einen|mit|für|auf|über|unter)\b",
    _re_lang.IGNORECASE,
)
_IT_MARKERS = _re_lang.compile(
    r"\b(?:io|tu|lui|lei|noi|voi|loro|non|sono|sei|è|siamo|siete|"
    r"ciao|grazie|prego|come|perché|"
    r"con|senza|per|su|in|da|fare|fatto)\b",
    _re_lang.IGNORECASE,
)


def detect_lang_simple(text: str) -> str:
    """Heuristic language detection. Returns ISO code or "" if uncertain.

    Conservative: only returns a result when one language's score is
    clearly dominant. False negatives ("") are safe — caller treats them
    as "no signal, leave the response alone". False positives are bad —
    they trigger a translation when the original was actually fine.
    """
    if not text:
        return ""
    # Skip very short fragments where the regex would over-match.
    cleaned = text.strip()
    if len(cleaned) < 12:
        return ""

    scores = {
        "es": len(_ES_MARKERS.findall(cleaned)),
        "en": len(_EN_MARKERS.findall(cleaned)),
        "pt": len(_PT_MARKERS.findall(cleaned)),
        "fr": len(_FR_MARKERS.findall(cleaned)),
        "de": len(_DE_MARKERS.findall(cleaned)),
        "it": len(_IT_MARKERS.findall(cleaned)),
    }
    # Sort by score; require top score >= 2 AND >= 2× runner-up.
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top, runner = ranked[0], ranked[1]
    if top[1] < 2:
        return ""
    if top[1] < 2 * max(runner[1], 1):
        return ""
    return top[0]


async def final_lang_guard(
    text: str,
    user_lang: str,
    model_manager,
    redis_url: str | None = None,
) -> tuple[str, bool]:
    """Phase 1 closure — final language guard.

    If the response language doesn't match user_lang, translate. Returns
    (text, was_translated). Skip when user_lang is empty/English, when
    text is too short to detect, or when detection fails. Log when applied.

    This is the LAST defence line. It runs after honesty layer + policy
    so it sees the final user-visible text. If the LLM ad-hoc generated
    a refusal in the wrong language (gpt-4o-mini quirk), this catches it.
    """
    norm_user = _normalise_lang(user_lang)
    if not norm_user or not text:
        return text, False
    if not text.strip() or len(text.strip()) < 12:
        return text, False

    detected = detect_lang_simple(text)
    if not detected or detected == norm_user:
        return text, False
    # Mismatch — translate to user_lang.
    try:
        translated = await translate(text, norm_user, model_manager, redis_url)
        if translated == text:
            return text, False
        logger.warning(
            "i18n.final_guard_applied",
            user_lang=norm_user,
            detected_lang=detected,
            src_chars=len(text),
            out_chars=len(translated),
        )
        await _bump_metric(redis_url, "final_guard_applied")
        return translated, True
    except Exception as e:
        logger.debug("i18n.final_guard_failed", error=str(e)[:120])
        return text, False
