"""Epistemic State — calibrated confidence per knowledge domain.

The agent tracks how well it performs in different domains and adjusts
its communication style accordingly: more assertive where it's confident,
more cautious where it has a track record of errors.

Redis key: agent:epistemic
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import structlog

logger = structlog.get_logger()

EPISTEMIC_KEY = "agent:epistemic"

# Keyword → domain mapping (ordered by specificity)
_DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "programming": [
        "python", "javascript", "typescript", "docker", "kubernetes", "bash", "shell",
        "sql", "api", "código", "code", "función", "function", "bug", "debug", "git",
        "deploy", "kubernetes", "react", "fastapi", "django", "postgresql", "redis",
        "json", "yaml", "xml", "html", "css", "linux", "terminal",
    ],
    "crypto_finance": [
        "btc", "bitcoin", "eth", "ethereum", "crypto", "cripto", "defi", "nft",
        "binance", "coinbase", "trading", "token", "blockchain", "wallet", "staking",
        "yield", "liquidez", "liquidation", "precio", "price", "usdt", "usdc",
    ],
    "finance_traditional": [
        "acciones", "bolsa", "stocks", "dividendos", "dividends", "forex", "fondo",
        "inversión", "investment", "banco", "bank", "crédito", "credit", "impuesto",
        "tax", "renta", "income", "presupuesto", "budget",
    ],
    "web_scraping": [
        "scrape", "scraping", "crawl", "beautifulsoup", "selenium", "playwright",
        "requests", "fetch", "html parse", "xpath", "css selector", "extraer",
        "extract", "raspar", "navegar", "browser",
    ],
    "legal": [
        "contrato", "contract", "ley", "law", "legal", "cláusula", "clause",
        "demanda", "lawsuit", "derecho", "right", "obligación", "obligation",
        "tribunal", "court", "abogado", "lawyer", "litigio", "litigation",
        "regulación", "regulation", "normativa",
    ],
    "medical_health": [
        "medicina", "medical", "síntoma", "symptom", "diagnóstico", "diagnosis",
        "medicamento", "medication", "enfermedad", "disease", "salud", "health",
        "doctor", "médico", "tratamiento", "treatment", "dosis", "dose",
        "cirugía", "surgery", "hospital",
    ],
    "data_analysis": [
        "dataset", "pandas", "numpy", "matplotlib", "análisis", "analysis",
        "estadística", "statistics", "gráfico", "chart", "visualización",
        "regression", "modelo", "machine learning", "ml", "datos", "data",
        "csv", "excel", "spreadsheet",
    ],
    "automation": [
        "automatizar", "automate", "scheduled", "tarea programada", "reminder",
        "cron", "webhook", "trigger", "pipeline", "workflow", "bot",
        "notificación", "notification", "alert", "alarma",
    ],
    "news_current_events": [
        "noticia", "news", "actual", "current", "hoy", "today", "política",
        "politics", "gobierno", "government", "elección", "election", "guerra",
        "war", "economía", "economy", "mercado", "market",
    ],
}

_DEFAULT_STATE = {
    "domain_confidence": {
        "programming": 0.90,
        "crypto_finance": 0.80,
        "web_scraping": 0.88,
        "automation": 0.85,
        "data_analysis": 0.75,
        "finance_traditional": 0.55,
        "news_current_events": 0.60,
        "legal": 0.35,
        "medical_health": 0.30,
    },
    "recent_errors": [],          # [domain] — last 10 error domains
    "recent_successes": [],       # [domain] — last 10 success domains
    "total_interactions": 0,
    "last_updated": "",
}


async def load(redis_url: str) -> dict:
    """Load epistemic state from Redis, merging with defaults."""
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(redis_url, decode_responses=True)
        try:
            raw = await r.get(EPISTEMIC_KEY)
            if raw:
                state = json.loads(raw)
                # Deep merge with defaults (new domains added to defaults)
                merged = dict(_DEFAULT_STATE)
                merged["domain_confidence"] = dict(_DEFAULT_STATE["domain_confidence"])
                merged["domain_confidence"].update(state.get("domain_confidence", {}))
                merged["recent_errors"] = state.get("recent_errors", [])
                merged["recent_successes"] = state.get("recent_successes", [])
                merged["total_interactions"] = state.get("total_interactions", 0)
                merged["last_updated"] = state.get("last_updated", "")
                return merged
        finally:
            await r.aclose()
    except Exception:
        pass
    return dict(_DEFAULT_STATE)


async def save(state: dict, redis_url: str) -> None:
    """Persist epistemic state to Redis."""
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(redis_url, decode_responses=True)
        try:
            state["last_updated"] = datetime.now(timezone.utc).isoformat()
            await r.set(EPISTEMIC_KEY, json.dumps(state))
        finally:
            await r.aclose()
    except Exception:
        pass


def detect_domains(text: str) -> list[str]:
    """Detect which knowledge domains are relevant to a piece of text."""
    text_lower = text.lower()
    detected = []
    for domain, keywords in _DOMAIN_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            detected.append(domain)
    return detected


async def record_outcome(
    text: str,
    success: bool,
    redis_url: str,
    adjustment: float = 0.015,
) -> None:
    """Update domain confidence based on skill execution outcome.

    Symmetric adjustment: ±0.015 per interaction with diminishing returns near
    bounds. Execution success/failure is a proxy for domain engagement, not
    ground-truth accuracy — weights are intentionally conservative.
    """
    domains = detect_domains(text)
    if not domains:
        return
    try:
        state = await load(redis_url)
        for domain in domains:
            current = state["domain_confidence"].get(domain, 0.5)
            if success:
                # Increase — diminishing returns, hard cap at 0.95 to prevent saturation
                state["domain_confidence"][domain] = min(0.95, current + adjustment * (1.0 - current))
                _append_bounded(state["recent_successes"], domain, max_len=10)
            else:
                # Decrease — symmetric, floor at 0.05
                state["domain_confidence"][domain] = max(0.05, current - adjustment * (current - 0.05))
                _append_bounded(state["recent_errors"], domain, max_len=10)
        state["total_interactions"] = state.get("total_interactions", 0) + 1
        await save(state, redis_url)
    except Exception:
        pass


def _append_bounded(lst: list, item: str, max_len: int) -> None:
    lst.append(item)
    if len(lst) > max_len:
        lst.pop(0)


async def get_confidence(domain: str, redis_url: str) -> float:
    """Get confidence score (0.0-1.0) for a specific domain."""
    try:
        state = await load(redis_url)
        return state["domain_confidence"].get(domain, 0.5)
    except Exception:
        return 0.5


def format_for_context(state: dict) -> str:
    """Format epistemic state as a compact context block for LLM injection.

    [ADVISORY] This block provides self-calibration hints to the LLM.
    It does NOT enforce or restrict skill execution — it is purely informational.
    """
    confs = state.get("domain_confidence", {})
    if not confs:
        return ""

    high = [d for d, c in confs.items() if c >= 0.80]
    medium = [d for d, c in confs.items() if 0.55 <= c < 0.80]
    low = [d for d, c in confs.items() if c < 0.55]

    # [ADVISORY] label makes it clear this is calibration data, not an enforcement rule
    lines = ["[EPISTEMIC STATE — ADVISORY — calibrate your certainty:]"]
    if high:
        lines.append(f"High confidence (you know this well): {', '.join(high)}")
    if medium:
        lines.append(f"Medium confidence (answer with nuance): {', '.join(medium)}")
    if low:
        lines.append(f"Low confidence (be honest, suggest verifying): {', '.join(low)}")

    recent_errors = state.get("recent_errors", [])
    if recent_errors:
        error_domains = list(set(recent_errors[-5:]))
        lines.append(f"Recent errors in: {', '.join(error_domains)} — be extra cautious here")

    return "\n".join(lines)
