"""Hard Context Reset — detects genuine intent switches and clears all flow state.

When a user changes to a completely different topic mid-conversation, this module:
1. Detects the domain shift using domain fingerprinting (no LLM calls)
2. Clears the active_flow Redis state for the chat
3. Returns a [CONTEXT RESET] block for system-prompt injection

This complements the Active Flow Context Lock (flow_state.py):
  - Flow Lock PREVENTS accidental domain switches on ambiguous messages
  - Context Reset ENFORCES domain switches on clearly new intents

Design principle: conservative false-positive rate. Reset only fires when the
new message is CLEARLY in a different domain AND contains no flow-recovery signals.

Public API
----------
is_intent_switch(new_text, active_flow)       → bool
detect_message_domain(text)                   → str | None
perform_hard_reset(redis_url, chat_id, ...)   → None (async)
build_context_reset_block(old, new)           → str
"""

from __future__ import annotations

import re

import redis.asyncio as aioredis
import structlog

logger = structlog.get_logger()


# ── Domain fingerprints ────────────────────────────────────────────────────────

_DOMAINS: dict[str, re.Pattern[str]] = {
    "crypto": re.compile(
        r"\b(?:btc|bitcoin|eth|ethereum|sol|solana|bnb|ada|xrp|doge|matic|avax|dot|link|"
        r"cripto|crypto|blockchain|defi|token|nft|altcoin|criptomoneda|satoshi|staking|"
        r"binance|coinbase|coingecko|kraken|bybit|wallet|"
        r"precio\s+(?:del?\s+)?(?:btc|eth|bitcoin|ethereum)|"
        r"cu[aá]nto\s+(?:vale|est[aá])\s+(?:el\s+)?(?:btc|eth|bitcoin|ethereum))\b",
        re.IGNORECASE,
    ),
    "weather": re.compile(
        r"\b(?:clima|weather|temperatura|temperature|lluvia|rain|soleado|sunny|"
        r"nublado|cloudy|pron[oó]stico|forecast|tormenta|storm|grados|degrees|"
        r"celsius|fahrenheit|viento|wind|hum[eé]dad|humidity|granizo|nieve|snow|"
        r"tiempo\s+(?:en|para|de|hace)\s+\w+|"
        r"qu[eé]\s+tiempo\s+hace)\b",
        re.IGNORECASE,
    ),
    "code": re.compile(
        r"\b(?:c[oó]digo|code|script|funci[oó]n|function|clase|class|variable|bug|error|"
        r"debug|programa(?:r)?|programaci[oó]n|programming|algoritmo|algorithm|"
        r"python|javascript|java|typescript|rust|golang|php|ruby|html|css|sql|"
        r"api\s+endpoint|framework|library|biblioteca|m[oó]dulo|module|"
        r"async|await|loop|bucle|array|lista\s+(?:de\s+)?(?:datos|items)|"
        r"c[oó]mo\s+(?:creo|hago|escribo|implemento)\s+(?:una?\s+)?(?:funci[oó]n|clase|script))\b",
        re.IGNORECASE,
    ),
    "shopping": re.compile(
        r"\b(?:comprar|buy|tienda|store|zapato|shoe|ropa|clothing|"
        r"marca|brand|talla|size|amazon|aliexpress|mercado\s*libre|falabella|"
        r"ripley|samsung|iphone|laptop|televisor|electrodom[eé]stico|"
        r"oferta|deal|descuento|discount|carrito|cart|producto\s+(?:que|para))\b",
        re.IGNORECASE,
    ),
    "food": re.compile(
        r"\b(?:receta|recipe|comida|food|cocinar|cook|ingrediente|ingredient|"
        r"restaurante|restaurant|plato|dish|men[uú]|comer|eat|bebida|drink|"
        r"postre|dessert|ensalada|salad|sopa|soup|carne|meat|pollo|chicken|"
        r"vegetariano|vegano|vegan|c[oó]mo\s+(?:preparo|cocino|hago)\s+\w+)\b",
        re.IGNORECASE,
    ),
    "news": re.compile(
        r"\b(?:noticia|news|titular|headline|peri[oó]dico|newspaper|revista|magazine|"
        r"pol[ií]tica|politics|gobierno|government|elecci[oó]n|election|presidente|"
        r"president|guerra|war|conflicto|econom[ií]a|economy|inflaci[oó]n|"
        r"qu[eé]\s+(?:pas[oó]|est[aá]\s+pasando)|[uú]ltimas?\s+noticias)\b",
        re.IGNORECASE,
    ),
    "system": re.compile(
        r"\b(?:agente|agent|objetivo|goal|habilidad|skill|memoria|memory|"
        r"capacidad|capability|configuraci[oó]n|config|prime\.md|orquestador|"
        r"orchestrator|planificador|planner|ejecutor|executor|sistema\s+(?:de|del))\b",
        re.IGNORECASE,
    ),
    "scheduling": re.compile(
        r"\b(?:recordatorio|reminder|alarma|alarm|agenda|tarea\s+programada|"
        r"programa(?:r)?\s+(?:una?\s+)?(?:tarea|alarma|notificaci[oó]n)|"
        r"cada\s+\d+\s+(?:minuto|hora|d[ií]a)|every\s+\d+\s+(?:min|hour|day)|"
        r"recurrente|recurring|automatiza)\b",
        re.IGNORECASE,
    ),
}

# Canonical domain aliases for the active_flow "domain" field
_FLOW_DOMAIN_MAP: dict[str, str] = {
    "crypto": "crypto",
    "bitcoin": "crypto",
    "ethereum": "crypto",
    "cripto": "crypto",
    "weather": "weather",
    "clima": "weather",
    "code": "code",
    "codigo": "code",
    "shopping": "shopping",
    "compras": "shopping",
    "food": "food",
    "comida": "food",
    "news": "news",
    "noticias": "news",
    "system": "system",
    "sistema": "system",
    "scheduling": "scheduling",
    "tareas": "scheduling",
}

# Recovery/continuation signals — if present, do NOT reset (user is following up)
_RECOVERY_SIGNAL_RE = re.compile(
    r"\b(?:"
    r"usa(?:r)?\s+(?:otra\s+)?(?:fuente|api|p[aá]gina|sitio)|"
    r"busca(?:r)?\s+(?:en\s+)?(?:otro\s+lado|otra\s+fuente|otro\s+sitio)|"
    r"intenta(?:r)?\s+(?:de\s+nuevo|otra\s+vez|con\s+(?:otro|otra))|"
    r"prueba(?:r)?\s+(?:con\s+)?(?:otro|otra)|"
    r"cambia(?:r)?\s+(?:la\s+)?(?:fuente|api|origen)|"
    r"change\s+(?:the\s+)?source|use\s+(?:another|different)\s+source|"
    r"s[ií](?:,)?\s+(?:puedes?|busca|intenta|prueba)|"
    r"claro(?:,)?\s+(?:busca|usa|intenta|prueba)|"
    r"ok(?:ay)?(?:,)?\s+(?:busca|usa|intenta|prueba|entonces)|"
    r"contin[uú]a|continue|sigue\s+(?:con|buscando)|keep\s+going|prosigue|"
    r"intenta\s+de\s+nuevo|try\s+again|vuelve\s+a\s+intentar"
    r")\b",
    re.IGNORECASE,
)


# ── Core detection functions ───────────────────────────────────────────────────

def detect_message_domain(text: str) -> str | None:
    """Return the dominant domain of a message, or None if ambiguous/general."""
    scores: dict[str, int] = {}
    for domain, pattern in _DOMAINS.items():
        matches = pattern.findall(text)
        if matches:
            scores[domain] = len(matches)
    if not scores:
        return None
    return max(scores, key=lambda d: scores[d])


def _normalize_flow_domain(flow_domain: str) -> str | None:
    """Map an active_flow domain string to a canonical domain key."""
    d = flow_domain.lower().strip()
    if d in _FLOW_DOMAIN_MAP:
        return _FLOW_DOMAIN_MAP[d]
    for key in _FLOW_DOMAIN_MAP:
        if key in d:
            return _FLOW_DOMAIN_MAP[key]
    return None


def is_intent_switch(new_text: str, active_flow: dict) -> bool:
    """Return True if the new message represents a genuinely different intent.

    Conditions for an intent switch (all must hold):
    1. The active flow has a known domain
    2. The new message has a clearly detectable domain
    3. The two domains differ
    4. The new message does NOT contain flow-recovery signals

    Conservative by design: if any condition is unclear, returns False (no reset).
    """
    flow_domain_raw = active_flow.get("domain", "")
    if not flow_domain_raw:
        return False

    flow_domain = _normalize_flow_domain(flow_domain_raw)
    if not flow_domain:
        return False

    new_domain = detect_message_domain(new_text)
    if not new_domain:
        return False  # Ambiguous → stay in current flow

    if new_domain == flow_domain:
        return False  # Same domain → not a switch

    if _RECOVERY_SIGNAL_RE.search(new_text):
        return False  # Continuation/recovery → not a switch

    return True


# ── Redis reset ────────────────────────────────────────────────────────────────

async def perform_hard_reset(
    redis_url: str,
    chat_id: str,
    old_domain: str,
    new_domain: str,
) -> None:
    """Clear all per-chat context state for a hard reset.

    Clears:
      - active_flow:{chat_id}     — the flow context lock
      - recovery_state:{chat_id} — any recovery retry flags

    Does NOT clear memory (episodic, KG, procedural) or scheduled tasks —
    those are cross-session and persistent by design.
    """
    if not redis_url or not chat_id:
        return
    keys = [
        f"active_flow:{chat_id}",
        f"recovery_state:{chat_id}",
    ]
    r = aioredis.from_url(redis_url, decode_responses=True)
    try:
        deleted = await r.delete(*keys)
        logger.info(
            "context_reset.performed",
            chat_id=str(chat_id),
            old_domain=old_domain,
            new_domain=new_domain,
            keys_cleared=deleted,
        )
    except Exception as exc:
        logger.warning("context_reset.failed", error=str(exc)[:80])
    finally:
        await r.aclose()


# ── Prompt injection ───────────────────────────────────────────────────────────

def build_context_reset_block(old_domain: str, new_domain: str) -> str:
    """System-prompt injection block that anchors the LLM to the NEW intent.

    Injected when an intent switch is detected. Tells the LLM to completely
    disregard the previous task/flow and focus only on the current message.
    """
    return (
        "\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "[CONTEXT RESET — NEW INTENT DETECTED]\n"
        f"Previous context: {old_domain.upper()}\n"
        f"Current request : {new_domain.upper()}\n"
        "\n"
        "MANDATORY RULES FOR THIS TURN:\n"
        "1. The user has switched to a COMPLETELY DIFFERENT topic.\n"
        "2. IGNORE all previous task context, flow state, and execution history.\n"
        "3. Do NOT reference, mention, or continue any previous unrelated task.\n"
        "4. Treat the current message as a fresh, standalone request.\n"
        "5. Focus EXCLUSIVELY on what the user is asking right now.\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
