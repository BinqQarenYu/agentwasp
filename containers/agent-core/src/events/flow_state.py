"""Active flow state — cross-turn context continuity.

When a workflow fails (e.g. missing crypto data), this module stores a
lightweight record of the active flow in Redis (TTL 15 min). The next
user message loads that record and the LLM is anchored to the same flow
instead of drifting to stale domains (weather, unrelated topics, etc.).

Public API
----------
save_active_flow(redis_url, chat_id, flow)  — call after failure
load_active_flow(redis_url, chat_id)         — call at start of next turn
clear_active_flow(redis_url, chat_id)        — call after success
is_explicit_domain_switch(text, flow)        — detect "forget that / new topic"
is_crypto_recovery_followup(text)            — detect source-change instructions
build_flow_context_block(flow)               — build system-prompt injection
"""

from __future__ import annotations

import json
import re
import time

import redis.asyncio as aioredis
import structlog

logger = structlog.get_logger()

_FLOW_KEY_PREFIX = "active_flow:"
_FLOW_TTL = 900  # 15 minutes

# ── Pattern: explicit domain switch away from the active flow ──────────────────
_DOMAIN_SWITCH_RE = re.compile(
    r"\b(?:"
    r"olvida(?:r)?(?:\s+(?:eso|todo|el\s+tema))?|"
    r"forget\s+(?:it|that|this|everything)|"
    r"cancela(?:r)?(?:\s+(?:eso|todo|el\s+informe))?|"
    r"cancel\s+(?:that|this)|"
    r"d[eé]ja(?:lo)?|deja\s+(?:eso|todo)|"
    r"never\s*mind|ya\s+no\s+(?:importa|quiero|me\s+interesa)|"
    r"cambia(?:r)?\s+de\s+tema|change\s+(?:the\s+)?subject|"
    r"nueva\s+pregunta|new\s+(?:question|topic)|"
    r"otra\s+cosa|something\s+else|"
    r"en\s+cambio\s+(?:dime|cu[eé]ntame)|"
    r"mejor\s+(?:dime|cu[eé]ntame|quiero\s+saber)"
    r")\b",
    re.IGNORECASE,
)

# ── Pattern: crypto-flow recovery instructions ────────────────────────────────
_CRYPTO_RECOVERY_RE = re.compile(
    r"\b(?:"
    r"coinbase|coinmarketcap|coingecko|binance|kraken|bybit|okx|gemini|bitfinex|"
    r"busca(?:r)?\s+(?:en\s+)?(?:otro\s+lado|otra\s+fuente|otra\s+p[aá]gina|otra\s+api)|"
    r"busca(?:r)?\s+la\s+informaci[oó]n|"
    r"puedes?\s+buscar\s+(?:en|la|esa|otra)|"
    r"usa(?:r)?\s+(?:otra\s+)?(?:fuente|api|p[aá]gina|sitio)|"
    r"use\s+(?:another|a\s+different)\s+(?:source|api)|"
    r"intenta(?:r)?\s+(?:de\s+nuevo|otra\s+vez|con\s+(?:otro|otra))|"
    r"prueba(?:r)?\s+(?:con\s+)?(?:otro|otra\s+(?:api|fuente))|"
    r"cambia(?:r)?\s+(?:la\s+)?(?:fuente|api|origen)|"
    r"change\s+(?:the\s+)?source|"
    r"s[ií](?:,)?\s+(?:puedes?|busca)|"          # "sí, puedes buscar..."
    r"claro(?:,)?\s+(?:busca|usa|intenta|prueba)"  # "claro, busca..."
    r")\b",
    re.IGNORECASE,
)

# ── Pattern: crypto domain keywords ───────────────────────────────────────────
_CRYPTO_ASSET_RE = re.compile(
    r"\b(btc|bitcoin|eth|ethereum|sol|solana|bnb|ada|xrp|doge|matic|avax|dot|link|"
    r"cripto|crypto)\b",
    re.IGNORECASE,
)

# ── Pattern: unrelated domain explicit mentions (weather, etc.) ───────────────
_UNRELATED_DOMAIN_RE = re.compile(
    r"\b(?:"
    r"clima\s+en|weather\s+in|weather\s+for|temperatura\s+en|"
    r"pron[oó]stico\s+(?:del\s+)?(?:tiempo|clima)|"
    r"forecast\s+for|lluvia\s+en|"
    r"noticias?\s+de\s+(?!cripto|crypto|bitcoin|btc|eth|ethereum)|"
    r"bolsa\s+de\s+valores|stock\s+market|"
    r"partido\s+de\s+|resultado\s+del\s+partido|"
    r"receta\s+de\s+|c[oó]mo\s+(?:cocinar|preparar)\s+"
    r")\b",
    re.IGNORECASE,
)


# ── Storage helpers ────────────────────────────────────────────────────────────

async def save_active_flow(redis_url: str, chat_id: str, flow: dict) -> None:
    """Persist active flow state for a chat. TTL = 15 minutes."""
    if not redis_url or not chat_id:
        return
    key = f"{_FLOW_KEY_PREFIX}{chat_id}"
    flow["stored_at"] = time.time()
    r = aioredis.from_url(redis_url, decode_responses=True)
    try:
        await r.set(key, json.dumps(flow), ex=_FLOW_TTL)
        logger.info(
            "active_flow.saved",
            chat_id=chat_id,
            domain=flow.get("domain"),
            flow_type=flow.get("flow_type"),
            assets=flow.get("assets"),
        )
    except Exception as exc:
        logger.warning("active_flow.save_failed", error=str(exc)[:80])
    finally:
        await r.aclose()


async def load_active_flow(redis_url: str, chat_id: str) -> dict | None:
    """Load active flow state for a chat. Returns None if not set or expired."""
    if not redis_url or not chat_id:
        return None
    key = f"{_FLOW_KEY_PREFIX}{chat_id}"
    r = aioredis.from_url(redis_url, decode_responses=True)
    try:
        val = await r.get(key)
        if not val:
            return None
        return json.loads(val)
    except Exception:
        return None
    finally:
        await r.aclose()


async def clear_active_flow(redis_url: str, chat_id: str) -> None:
    """Clear active flow state (call after successful execution)."""
    if not redis_url or not chat_id:
        return
    key = f"{_FLOW_KEY_PREFIX}{chat_id}"
    r = aioredis.from_url(redis_url, decode_responses=True)
    try:
        deleted = await r.delete(key)
        if deleted:
            logger.info("active_flow.cleared", chat_id=chat_id)
    except Exception:
        pass
    finally:
        await r.aclose()


# ── Intent classifiers ────────────────────────────────────────────────────────

def is_explicit_domain_switch(text: str, active_flow: dict) -> bool:
    """Return True if the user is clearly abandoning the active flow.

    Both conditions must be true:
    1. Explicit cancel/forget language present ("olvida eso", "forget it", etc.)
    2. An explicit unrelated domain is mentioned ("clima en", "weather in", etc.)

    A bare "olvida" or "cancela el informe" without a new topic stays in the flow
    (could be a correction or retry, not a domain change).
    """
    has_cancel = bool(_DOMAIN_SWITCH_RE.search(text))
    if not has_cancel:
        return False
    # Require an explicit unrelated domain to confirm the switch
    return bool(_UNRELATED_DOMAIN_RE.search(text))


def is_crypto_recovery_followup(text: str) -> bool:
    """Return True if the message looks like a recovery instruction for a crypto flow."""
    return bool(_CRYPTO_RECOVERY_RE.search(text))


def detect_flow_assets(text: str) -> list[str]:
    """Extract crypto asset tickers from text (deduped, uppercased)."""
    _ALIAS = {
        "btc": "BTC", "bitcoin": "BTC",
        "eth": "ETH", "ethereum": "ETH",
        "sol": "SOL", "solana": "SOL",
        "bnb": "BNB",
        "ada": "ADA", "cardano": "ADA",
        "xrp": "XRP", "ripple": "XRP",
        "doge": "DOGE", "dogecoin": "DOGE",
        "matic": "MATIC", "polygon": "MATIC",
        "avax": "AVAX", "avalanche": "AVAX",
        "dot": "DOT", "polkadot": "DOT",
        "link": "LINK", "chainlink": "LINK",
    }
    found: list[str] = []
    for m in _CRYPTO_ASSET_RE.finditer(text):
        ticker = _ALIAS.get(m.group(1).lower(), m.group(1).upper())
        if ticker not in found and ticker not in ("CRIPTO",):
            found.append(ticker)
    return found


# ── Context block builder ──────────────────────────────────────────────────────

def build_flow_context_block(active_flow: dict) -> str:
    """Build the system-prompt injection block for an active flow.

    This block is appended to the system prompt to anchor the LLM to the
    correct domain and prevent cross-domain contamination.
    """
    domain = active_flow.get("domain", "unknown")
    flow_type = active_flow.get("flow_type", "WORKFLOW")
    assets = active_flow.get("assets", [])
    delivery = active_flow.get("delivery", [])
    last_failure = active_flow.get("last_failure", "datos incompletos")
    instruction = active_flow.get("instruction", "")

    assets_str = ", ".join(assets) if assets else "cripto"
    delivery_str = ", ".join(delivery) if delivery else "email"

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "[ACTIVE FLOW — CONTEXT LOCK]",
        f"Domain  : {domain.upper()}",
        f"Flow    : {flow_type}",
        f"Assets  : {assets_str}",
        f"Delivery: {delivery_str}",
        f"Failure : {last_failure}",
    ]
    if instruction:
        lines.append(f"Task    : {instruction[:140]}")
    lines += [
        "",
        "MANDATORY RULES FOR THIS TURN:",
        "1. The user's message is a follow-up to the FAILED workflow above.",
        "2. Interpret it as a modification, source change, or recovery instruction for the SAME flow.",
        "3. DO NOT answer about weather, news, stocks, or any unrelated domain.",
        "4. DO NOT switch domain unless the user explicitly says 'forget that' or starts a clearly new topic.",
        "5. If the user suggests a different data source (coinbase, coingecko, etc.), retry the SAME workflow with that source.",
        "6. If the user's intent is unclear, respond ONLY with:",
        '   "Sigo dentro del flujo del informe cripto. Puedo cambiar la fuente de datos a CoinGecko, CoinMarketCap o Coinbase. ¿Cuál prefieres?"',
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    return "\n".join(lines)
