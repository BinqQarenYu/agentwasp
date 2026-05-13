"""Opportunity Engine — proactive automation detection from user behavior patterns.

v1 (preserved): keyword-based repetition detection → fixed suggestion templates
v2 (additive):  temporal clustering + entity extraction + intent inference → smart suggestions

Scans episodic memory every 2 hours via OpportunityEngineJob.

Pattern detection:
  v1: 3+ similar requests within 24h → keyword-matched opportunity type
  v2: related actions clustered by time + shared entities → inferred workflow

Rate limiting (unchanged):
  - Max 2 suggestions per user per day
  - No repeat of same opportunity type within 48h

Memory (Redis — no schema changes):
  opp:daily:{user_id}:{YYYYMMDD}         TTL 86400s  — daily suggestion counter
  opp:suggested:{user_id}:{type}:{day}   TTL 172800s — 48h dedup flag
  opp:queue:{user_id}                    TTL 86400s  — overflow queue (JSON list)
  opp:pattern:{fingerprint}              TTL 604800s — known pattern (7-day, dedup discovery)
"""
from __future__ import annotations

import hashlib
import json
import re
import structlog
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any

import redis.asyncio as aioredis

from .db.session import async_session
from .events.bus import EventBus
from .events.types import EventType
from .memory.types import MemoryQuery, MemoryType
from .models.manager import ModelManager
from .models.types import Message, ModelRequest
from .utils.safe_notify import safe_notify

logger = structlog.get_logger()

MAX_SUGGESTIONS_PER_DAY = 3
DEDUP_WINDOW_HOURS = 48
PATTERN_THRESHOLD = 3          # Minimum occurrences to call it a pattern
PATTERN_WINDOW_HOURS = 24      # Look-back window for pattern detection

# ─── v2 clustering constants ────────────────────────────────────────────────
CLUSTER_WINDOW_HOURS = 3       # Events within 3h are considered part of same session
CLUSTER_MIN_SIZE = 2           # Minimum events in a cluster to consider it a workflow
CLUSTER_CONFIDENCE_THRESHOLD = 0.55  # Minimum confidence to generate a v2 suggestion

# ─── Opportunity type definitions (v1 — unchanged) ──────────────────────────

OPPORTUNITY_TYPES: list[dict] = [
    {
        "type": "crypto_monitoring",
        "name": "crypto price monitoring",
        "keywords": [
            "btc", "eth", "bitcoin", "ethereum", "crypto", "price", "precio",
            "criptomoneda", "binance", "coinbase", "solana", "sol", "xrp",
            "market", "mercado cripto",
        ],
        "suggestion": (
            "Noté que consultás frecuentemente los precios de criptomonedas.\n\n"
            "Podría automatizar esto para ti:\n"
            "• Monitorear BTC, ETH y otras cripto en tiempo real\n"
            "• Detectar movimientos grandes del mercado\n"
            "• Enviarte alertas cuando haya cambios importantes\n\n"
            "¿Quieres que configure esto automáticamente?"
        ),
        "skills_involved": ["subscribe", "task_manager"],
    },
    {
        "type": "news_monitoring",
        "name": "AI/tech news monitoring",
        "keywords": [
            "news", "noticias", "ai news", "tech news", "headlines", "titulares",
            "openai", "google", "anthropic", "artificial intelligence",
            "inteligencia artificial", "machine learning", "latest",
        ],
        "suggestion": (
            "Noté que consultás frecuentemente noticias de tecnología e IA.\n\n"
            "Podría automatizar esto para ti:\n"
            "• Monitorear las últimas noticias de IA y tecnología\n"
            "• Enviarte un resumen diario de los titulares más relevantes\n"
            "• Alertarte sobre noticias importantes al instante\n\n"
            "¿Quieres que configure un monitor de noticias?"
        ),
        "skills_involved": ["subscribe", "task_manager"],
    },
    {
        "type": "website_monitoring",
        "name": "website change monitoring",
        "keywords": [
            "website", "sitio", "page", "página", "check site", "monitor site",
            "web change", "cambio en", "url", "http", "https",
        ],
        "suggestion": (
            "Noté que revisás cambios en sitios web con frecuencia.\n\n"
            "Podría automatizar esto para ti:\n"
            "• Monitorear los sitios que te interesan en segundo plano\n"
            "• Detectar cambios automáticamente\n"
            "• Enviarte una alerta cuando algo cambie\n\n"
            "¿Quieres que configure un monitor de páginas web?"
        ),
        "skills_involved": ["subscribe", "task_manager"],
    },
    {
        "type": "daily_report",
        "name": "scheduled daily report",
        "keywords": [
            "report", "reporte", "resumen", "summary", "daily", "diario",
            "informe", "resume", "análisis", "analysis", "briefing",
        ],
        "suggestion": (
            "Noté que pedís resúmenes e informes con regularidad.\n\n"
            "Podría automatizar esto para ti:\n"
            "• Generar un reporte diario personalizado cada mañana\n"
            "• Incluir métricas, noticias o cualquier dato que necesites\n"
            "• Enviártelo automáticamente a la hora que prefieras\n\n"
            "¿Quieres que configure un reporte diario automático?"
        ),
        "skills_involved": ["task_manager"],
    },
    {
        "type": "api_tracking",
        "name": "API data tracking",
        "keywords": [
            "api", "endpoint", "data", "datos", "tracking", "rastrear",
            "fetch", "request", "json", "rest",
        ],
        "suggestion": (
            "Noté que consultás APIs o datos externos con frecuencia.\n\n"
            "Podría automatizar esto para ti:\n"
            "• Consultar los datos automáticamente a intervalos regulares\n"
            "• Almacenar el historial para comparación\n"
            "• Enviarte alertas cuando los datos cambien\n\n"
            "¿Quieres que configure un tracker de datos automático?"
        ),
        "skills_involved": ["task_manager", "subscribe"],
    },
]

# Build reverse lookup: keyword → type_id (v1)
_KEYWORD_INDEX: dict[str, str] = {}
for _opp in OPPORTUNITY_TYPES:
    for _kw in _opp["keywords"]:
        _KEYWORD_INDEX[_kw.lower()] = _opp["type"]

# ─── v2 Entity & skill extraction patterns ──────────────────────────────────

_CRYPTO_RE = re.compile(
    r"\b(btc|eth|bitcoin|ethereum|sol|solana|bnb|xrp|ada|doge|link|dot|avax|matic|usdt)\b",
    re.IGNORECASE,
)
_URL_DOMAIN_RE = re.compile(r"https?://(?:www\.)?([a-zA-Z0-9\-]+\.[a-zA-Z]{2,})")
# Keyword signals in user_input → inferred skill (replaces agent_response marker approach which never matched)
_SKILL_KEYWORDS: list[tuple[str, re.Pattern]] = [
    ("web_search",   re.compile(r"\b(busca|search|google|find|buscar|investigar|noticias|news|headlines)\b", re.IGNORECASE)),
    ("browser",      re.compile(r"\b(screenshot|captura|pagina web|website|sitio|abre|navega)\b|https?://", re.IGNORECASE)),
    ("http_request", re.compile(r"\b(api|endpoint|json|rest)\b", re.IGNORECASE)),
    ("fetch_url",    re.compile(r"https?://", re.IGNORECASE)),
]
_TOPIC_RE = re.compile(
    r"\b(?:precio|price|cost[eo]|valor)\s+(?:de\s+|of\s+)?([a-zA-Z]{2,12})\b",
    re.IGNORECASE,
)
_EMAIL_SIGNAL_RE = re.compile(
    r"\b(gmail|send\s+email|enviar?\s+correo|correo\s+enviado|email\s+sent|mail)\b",
    re.IGNORECASE,
)

# ─── Sequence → intent mapping (ordered by specificity) ─────────────────────
# Each entry: (required_skills_subset, intent_id, workflow_label_es)
_SEQUENCE_INTENTS = [
    ({"gmail", "browser"},     "reporting",     "captura web y reporte"),
    ({"gmail", "fetch_url"},   "reporting",     "monitoreo web con reporte"),
    ({"gmail", "web_search"},  "reporting",     "investigación y reporte"),
    ({"gmail", "http_request"},"reporting",     "datos con reporte"),
    ({"browser", "web_search"},"research",      "investigación web"),
    ({"browser"},              "monitoring",    "monitoreo de sitio web"),
    ({"fetch_url"},            "monitoring",    "monitoreo de datos"),
    ({"web_search"},           "research",      "búsqueda de información"),
    ({"http_request"},         "api_tracking",  "seguimiento de API"),
    ({"gmail"},                "reporting",     "reporte por correo"),
]

# Intent → Spanish label
_INTENT_LABELS = {
    "monitoring":    "monitoreo automático",
    "reporting":     "generación de reportes",
    "research":      "investigación periódica",
    "api_tracking":  "seguimiento de datos",
    "tracking":      "seguimiento",
}

# ─── Suggested schedule per intent ──────────────────────────────────────────
_INTENT_SCHEDULE = {
    "monitoring":   "cada 4 horas",
    "reporting":    "una vez al día",
    "research":     "cada 12 horas",
    "api_tracking": "cada hora",
    "tracking":     "cada 6 horas",
}


class OpportunityEngine:
    """Scans memory for repeated patterns and suggests automation opportunities.

    v2 architecture:
        run()
          ├── _detect_opportunities_v2()   ← clustering + intent inference (new)
          │     ├── _load_episodic_events() ← parses memory into rich events
          │     ├── _cluster_events()       ← temporal + entity grouping
          │     ├── _score_cluster()        ← confidence scoring
          │     └── _build_smart_suggestion() ← dynamic text generation
          └── _detect_opportunities()      ← v1 keyword fallback (unchanged)
    """

    def __init__(
        self,
        memory,
        model_manager: ModelManager,
        redis_url: str,
        bus: EventBus,
        notify_chat_id: str = "",
        governor=None,
    ):
        self.memory = memory
        self.model_manager = model_manager
        self.redis_url = redis_url
        self.bus = bus
        self.notify_chat_id = notify_chat_id
        self.governor = governor

    # ──────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────

    async def run(self) -> str:
        """Full scan-detect-suggest cycle. Returns status string."""
        if not self.notify_chat_id:
            return "opportunity_engine: no chat_id configured"

        # CPI throttle — skip when system is under heavy load
        try:
            from .agent.cpi import is_high as _cpi_high
            if await _cpi_high(self.redis_url):
                logger.info("opportunity_engine.cpi_throttled")
                return "opportunity_engine: skipped (CPI high)"
        except Exception:
            pass

        user_id = self.notify_chat_id

        if await self._is_rate_limited(user_id):
            logger.info("opportunity_engine.rate_limited", user_id=user_id)
            return "opportunity_engine: rate limited"

        # v2: clustering-based detection (higher quality, tried first)
        v2_opportunities = await self._detect_opportunities_v2()

        # v1: keyword-based detection (fallback, always runs)
        v1_opportunities = await self._detect_opportunities()

        # Phase C: new signal sources — reflections + KG
        reflection_opportunities = await self._detect_from_reflections()
        kg_opportunities = await self._detect_from_kg()

        # Merge all sources: v2 > reflection > KG > v1; deduplicate by type
        seen_types: set[str] = set()
        opportunities: list[dict] = []
        for opp in v2_opportunities + reflection_opportunities + kg_opportunities + v1_opportunities:
            if opp["type"] not in seen_types:
                seen_types.add(opp["type"])
                # Tag source for those that don't already have it
                if "source" not in opp:
                    opp["source"] = "pattern"
                # Phase C: unified scoring + filter < 0.6
                score = self._score_opportunity(opp)
                if score == 0.0:
                    continue
                opp["confidence"] = score
                # Phase C: assign action policy
                opp["action_policy"] = self._assign_action_policy(opp)
                opportunities.append(opp)

        if not opportunities:
            return "opportunity_engine: no patterns detected"

        # Process queued opportunities from previous cycle first
        queued = await self._dequeue_opportunity(user_id)
        if queued:
            opportunities = [queued] + opportunities

        suggested_count = 0
        for opp in opportunities:
            if suggested_count >= MAX_SUGGESTIONS_PER_DAY:
                await self._enqueue_opportunity(user_id, opp)
                logger.info(
                    "opportunity_rate_limited",
                    user_id=user_id,
                    opportunity_type=opp["type"],
                    confidence_score=opp.get("confidence", 0),
                )
                continue

            if await self._was_suggested_recently(user_id, opp["type"]):
                logger.info(
                    "opportunity_skipped",
                    user_id=user_id,
                    opportunity_type=opp["type"],
                    reason="suggested_recently",
                )
                continue

            if self.governor and opp.get("skills_involved"):
                allowed = True
                for skill in opp["skills_involved"]:
                    action_type = "create_task" if "task" in skill else "create_goal"
                    ok, _ = await self.governor.check_allow(action_type, user_id=user_id)
                    if not ok:
                        allowed = False
                        break
                if not allowed:
                    logger.info(
                        "opportunity_skipped",
                        user_id=user_id,
                        opportunity_type=opp["type"],
                        reason="governor_limit",
                    )
                    continue

            # Phase C: persist to DB before acting
            fingerprint = self._opportunity_fingerprint(opp)
            record_id = await self._store_opportunity(opp, fingerprint)

            if opp.get("action_policy") == "draft_goal":
                # Fix 1: defer as pending_goal — do NOT send to user
                await self._set_opportunity_status(record_id, "pending_goal")
                logger.info(
                    "opportunity.deferred_as_goal",
                    user_id=user_id,
                    opportunity_type=opp["type"],
                    source=opp.get("source", "pattern"),
                    confidence_score=opp.get("confidence", 0),
                    record_id=record_id,
                )
            else:
                await self._send_suggestion(opp)
                await self._mark_suggested(user_id, opp["type"])
                await self._increment_daily_count(user_id)
                await self._mark_opportunity_seen(record_id)
                suggested_count += 1
                logger.info(
                    "opportunity_suggested",
                    user_id=user_id,
                    opportunity_type=opp["type"],
                    source=opp.get("source", "pattern"),
                    action_policy=opp.get("action_policy", "suggest_only"),
                    confidence_score=opp.get("confidence", 0),
                )

        return f"opportunity_engine: {suggested_count} suggestion(s) sent"

    # ──────────────────────────────────────────────────────────────────────
    # Phase C — New Signal Sources
    # ──────────────────────────────────────────────────────────────────────

    async def _detect_from_reflections(self) -> list[dict]:
        """Detect opportunities from execution reflection patterns.

        Reads recurring_pattern=True rows from execution_reflections.
        Groups by pattern_key, surfaces correction (failures) and
        optimization (low efficiency) opportunities.
        """
        try:
            from .db.models import ExecutionReflection
            from .db.session import async_session as _async_session
            from sqlalchemy import select as _select

            async with _async_session() as session:
                rows = (await session.execute(
                    _select(ExecutionReflection)
                    .where(ExecutionReflection.recurring_pattern == True)
                    .order_by(ExecutionReflection.timestamp.desc())
                    .limit(30)
                )).scalars().all()

            if not rows:
                return []

            # Group by pattern_key
            groups: dict[str, list] = {}
            for r in rows:
                pk = r.pattern_key or "unknown"
                groups.setdefault(pk, []).append(r)

            opps = []
            for pattern_key, group in groups.items():
                failures = [r for r in group if not r.success or r.efficiency_score < 0.7]
                if not failures:
                    continue

                all_fail = all(not r.success for r in failures)
                opp_type = "correction" if all_fail else "optimization"
                insight = (failures[0].insight or "").strip()
                if not insight or len(insight) < 10:
                    continue
                suggestion = (failures[0].suggestion or "").strip()
                entities = list({r.intent[:60] for r in failures[:3] if r.intent})

                confidence = round(min(1.0, len(failures) / 3 * 0.55 + 0.45), 2)
                opps.append({
                    "type":             opp_type,
                    "description":      insight,
                    "suggestion":       suggestion or insight,
                    "confidence":       confidence,
                    "source":           "reflection",
                    "related_entities": entities[:3],
                    "hits":             len(failures),
                    "name":             f"{opp_type}: {pattern_key[:40]}",
                    "skills_involved":  [],
                    "v2":               False,
                })

            return [o for o in opps if o["confidence"] >= 0.6]

        except Exception as exc:
            logger.debug("opportunity_engine.reflections_detect_failed", error=str(exc)[:80])
            return []

    async def _detect_from_kg(self) -> list[dict]:
        """Detect automation opportunities from KG insight snapshot.

        Reads kg:insights from Redis (written by KgInsightsUpdaterJob every 30min).
        A dominant tech stack of 2+ tools signals a repeating workflow.
        """
        try:
            raw = None
            r = aioredis.from_url(self.redis_url, decode_responses=True)
            try:
                raw = await r.get("kg:insights")
            finally:
                await r.aclose()

            if not raw:
                return []

            insights = json.loads(raw)
            dominant_stack = insights.get("dominant_stack", [])
            top_tools = insights.get("top_tools", [])

            if len(dominant_stack) < 2 and len(top_tools) < 2:
                return []

            stack = dominant_stack[:3] or top_tools[:3]
            stack_str = ", ".join(stack)
            confidence = round(min(0.85, 0.55 + len(stack) * 0.10), 2)

            return [{
                "type":             "automation",
                "description":      f"Tecnologías usadas frecuentemente: {stack_str}. Podría automatizar un flujo de trabajo recurrente.",
                "suggestion":       (
                    f"Noté que trabajás frecuentemente con {stack_str}.\n\n"
                    f"Puedo crear un flujo de trabajo automático para ti:\n"
                    f"• Ejecutar tareas recurrentes con tu stack actual\n"
                    f"• Notificarte con los resultados automáticamente\n\n"
                    f"¿Quieres que configure una tarea programada con estas herramientas?"
                ),
                "confidence":       confidence,
                "source":           "KG",
                "related_entities": stack,
                "hits":             len(stack),
                "name":             f"tech stack workflow: {stack[0]}",
                "skills_involved":  ["task_manager"],
                "v2":               False,
            }]

        except Exception as exc:
            logger.debug("opportunity_engine.kg_detect_failed", error=str(exc)[:80])
            return []

    # ──────────────────────────────────────────────────────────────────────
    # Phase C — Scoring, Policy, Storage
    # ──────────────────────────────────────────────────────────────────────

    def _score_opportunity(self, opp: dict) -> float:
        """Unified opportunity score combining base confidence, frequency, and source quality.

        Returns 0.0 if the opportunity is below the 0.6 minimum threshold.
        """
        base = float(opp.get("confidence", 0.0))
        hits = int(opp.get("hits", 1))
        source = opp.get("source", "pattern")

        # Frequency bonus: each additional hit adds 0.04, capped at +0.20
        freq_bonus = min(0.20, max(0, hits - 1) * 0.04)
        # Source quality: reflection evidence is stronger than pure keyword matching
        source_bonus = {"reflection": 0.10, "KG": 0.05, "pattern": 0.0}.get(source, 0.0)

        score = min(1.0, base + freq_bonus + source_bonus)
        return round(score, 2) if score >= 0.6 else 0.0

    def _assign_action_policy(self, opp: dict) -> str:
        """Assign action policy based on opportunity type, confidence, and source.

        Safety rules:
        - Correction opportunities are always suggest_only (human must review)
        - draft_goal only for high-confidence automation from known sources
        - auto_execute disabled (no trivially safe scenarios identified)
        """
        opp_type = opp.get("type", "suggestion")
        confidence = float(opp.get("confidence", 0.0))
        source = opp.get("source", "pattern")

        if opp_type == "correction":
            return "suggest_only"
        if opp_type == "automation" and confidence >= 0.80 and source in ("KG", "pattern"):
            return "draft_goal"
        return "suggest_only"

    def _opportunity_fingerprint(self, opp: dict) -> str:
        """Content-based fingerprint for dedup — hash of type + description + entities."""
        desc = (opp.get("description") or "")[:100].strip()
        entities = sorted(str(e) for e in (opp.get("related_entities") or []))[:3]
        raw = f"{opp.get('type', '')}:{desc}:{','.join(entities)}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    async def _store_opportunity(self, opp: dict, fingerprint: str) -> str | None:
        """Upsert opportunity to DB. Returns record ID.

        Fix 2: 24h fingerprint cooldown — if the same content fingerprint was
        stored within the last 24 hours, return its ID without a new insert.
        After 24h the opportunity can reappear as a fresh record.
        """
        try:
            from .db.models import Opportunity as _OppModel
            from .db.session import async_session as _async_session
            from sqlalchemy import select as _select
            from uuid import uuid4 as _uuid4

            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

            async with _async_session() as session:
                existing = (await session.execute(
                    _select(_OppModel).where(
                        _OppModel.fingerprint == fingerprint,
                        _OppModel.created_at > cutoff,
                    ).limit(1)
                )).scalar_one_or_none()

                if existing:
                    return existing.id

                new_rec = _OppModel(
                    id=str(_uuid4()),
                    opp_type=opp.get("type", "suggestion"),
                    description=(opp.get("description") or opp.get("suggestion", ""))[:500],
                    confidence=float(opp.get("confidence", 0.0)),
                    source=opp.get("source", "pattern"),
                    related_entities=(opp.get("related_entities") or [])[:5],
                    action_policy=opp.get("action_policy", "suggest_only"),
                    status="new",
                    fingerprint=fingerprint,
                )
                session.add(new_rec)
                await session.commit()
                return new_rec.id

        except Exception as exc:
            logger.debug("opportunity_engine.store_failed", error=str(exc)[:80])
            return None

    async def _mark_opportunity_seen(self, record_id: str) -> None:
        """Set status → seen and record suggested_at timestamp."""
        await self._set_opportunity_status(record_id, "seen", set_suggested_at=True)

    async def _set_opportunity_status(
        self, record_id: str, status: str, set_suggested_at: bool = False
    ) -> None:
        """Generic status setter for stored opportunities."""
        if not record_id:
            return
        try:
            from .db.models import Opportunity as _OppModel
            from .db.session import async_session as _async_session

            async with _async_session() as session:
                rec = await session.get(_OppModel, record_id)
                if rec:
                    rec.status = status
                    if set_suggested_at:
                        rec.suggested_at = datetime.now(timezone.utc)
                    await session.commit()
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────────────────
    # V1 — Keyword Pattern Detection (unchanged)
    # ──────────────────────────────────────────────────────────────────────

    async def _detect_opportunities(self) -> list[dict]:
        """v1: Scan episodic memory and return detected opportunities sorted by confidence."""
        try:
            async with async_session() as session:
                rows = await self.memory.retrieve(
                    session,
                    MemoryQuery(
                        memory_type=MemoryType.EPISODIC,
                        limit=50,
                    ),
                )
        except Exception as exc:
            logger.debug("opportunity_engine.memory_scan_failed", error=str(exc)[:120])
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(hours=PATTERN_WINDOW_HOURS)
        recent_inputs: list[str] = []
        for row in rows:
            try:
                ts_raw = row.content.get("timestamp", "")
                if ts_raw:
                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    if ts < cutoff:
                        continue
                user_input = row.content.get("user_input", "").lower()
                if user_input:
                    recent_inputs.append(user_input)
            except Exception:
                continue

        if not recent_inputs:
            return []

        type_hits: dict[str, int] = {}
        for text in recent_inputs:
            seen_types: set[str] = set()
            for kw, opp_type in _KEYWORD_INDEX.items():
                if kw in text and opp_type not in seen_types:
                    type_hits[opp_type] = type_hits.get(opp_type, 0) + 1
                    seen_types.add(opp_type)

        opportunities: list[dict] = []
        opp_lookup = {o["type"]: o for o in OPPORTUNITY_TYPES}
        for opp_type, hits in sorted(type_hits.items(), key=lambda x: -x[1]):
            if hits >= PATTERN_THRESHOLD:
                opp_def = opp_lookup.get(opp_type, {})
                confidence = min(1.0, hits / (PATTERN_THRESHOLD * 2))
                opportunities.append({
                    "type": opp_type,
                    "name": opp_def.get("name", opp_type),
                    "hits": hits,
                    "confidence": round(confidence, 2),
                    "suggestion": opp_def.get("suggestion", ""),
                    "skills_involved": opp_def.get("skills_involved", []),
                    "v2": False,
                })
                logger.info(
                    "opportunity_detected",
                    opportunity_type=opp_type,
                    hits=hits,
                    confidence_score=round(confidence, 2),
                )

        return opportunities

    # ──────────────────────────────────────────────────────────────────────
    # V2 — Clustering & Intent Inference
    # ──────────────────────────────────────────────────────────────────────

    async def _detect_opportunities_v2(self) -> list[dict]:
        """v2: Cluster related actions by time + entities, infer intent, build smart suggestions."""
        events = await self._load_episodic_events()
        if len(events) < CLUSTER_MIN_SIZE:
            return []

        clusters = self._cluster_events(events)
        if not clusters:
            return []

        # Load previously discovered pattern fingerprints to avoid redundant suggestions
        known_patterns = await self._load_known_patterns()

        opportunities: list[dict] = []
        for cluster in clusters:
            if len(cluster) < CLUSTER_MIN_SIZE:
                continue

            intent, workflow_label = self._infer_intent(cluster)
            score = self._score_cluster(cluster, intent)

            # Structured trace log for observability (Phase 7)
            entities = self._extract_cluster_entities(cluster)
            skills = self._extract_cluster_skills(cluster)
            logger.info(
                "opportunity.trace",
                detected_pattern=workflow_label,
                cluster_size=len(cluster),
                inferred_intent=intent,
                confidence_score=round(score, 2),
                entities=sorted(entities)[:5],
                skills=sorted(skills),
                suggestion_generated=score >= CLUSTER_CONFIDENCE_THRESHOLD,
            )

            if score < CLUSTER_CONFIDENCE_THRESHOLD:
                continue

            # Build a stable fingerprint from intent + top entities (for dedup)
            fingerprint = self._cluster_fingerprint(intent, entities)
            if fingerprint in known_patterns:
                logger.info(
                    "opportunity.trace.skipped_known",
                    fingerprint=fingerprint[:12],
                    intent=intent,
                )
                continue

            # Build the opportunity dict in v1-compatible format
            opp_type = f"workflow_{intent}"
            suggestion = self._build_smart_suggestion(cluster, intent, workflow_label, entities, skills)
            if not suggestion:
                continue

            # Persist this pattern so we don't rediscover it next cycle
            await self._save_pattern_memory(fingerprint, intent, entities)

            opportunities.append({
                "type": opp_type,
                "name": f"detected workflow: {workflow_label}",
                "hits": len(cluster),
                "confidence": round(score, 2),
                "suggestion": suggestion,
                "skills_involved": ["task_manager"],
                "v2": True,
                "intent": intent,
                "entities": sorted(entities)[:5],
            })

        # Sort by confidence descending
        opportunities.sort(key=lambda x: -x["confidence"])
        return opportunities

    def _load_episodic_events_sync(self, rows: list) -> list[dict]:
        """Parse memory rows into structured events (timestamp, user_input, skills, entities)."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=PATTERN_WINDOW_HOURS)
        events: list[dict] = []
        for row in rows:
            try:
                content = row.content or {}
                ts_raw = content.get("timestamp", "")
                if not ts_raw:
                    continue
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                if ts < cutoff:
                    continue

                user_input = content.get("user_input", "")
                agent_response = content.get("agent_response", "")

                # Infer skills from user_input keywords (reliable; agent_response markers don't exist)
                skills: set[str] = set()
                for _skill_name, _skill_pat in _SKILL_KEYWORDS:
                    if _skill_pat.search(user_input):
                        skills.add(_skill_name)

                # Extract entities from user_input
                entities: set[str] = set()
                for m in _CRYPTO_RE.finditer(user_input):
                    entities.add(m.group(1).lower())
                for m in _URL_DOMAIN_RE.finditer(user_input):
                    entities.add(m.group(1).lower())
                for m in _TOPIC_RE.finditer(user_input):
                    entities.add(m.group(1).lower())

                # Email signal
                has_email = bool(_EMAIL_SIGNAL_RE.search(user_input) or _EMAIL_SIGNAL_RE.search(agent_response))
                if has_email:
                    skills.add("gmail")

                if not user_input:
                    continue

                events.append({
                    "ts": ts,
                    "user_input": user_input.lower(),
                    "skills": skills,
                    "entities": entities,
                    "has_email": has_email,
                })
            except Exception:
                continue

        events.sort(key=lambda e: e["ts"])
        return events

    async def _load_episodic_events(self) -> list[dict]:
        """Retrieve and parse episodic memory into structured events."""
        try:
            async with async_session() as session:
                rows = await self.memory.retrieve(
                    session,
                    MemoryQuery(memory_type=MemoryType.EPISODIC, limit=60),
                )
            return self._load_episodic_events_sync(rows)
        except Exception as exc:
            logger.debug("opportunity_engine_v2.load_failed", error=str(exc)[:120])
            return []

    def _cluster_events(self, events: list[dict]) -> list[list[dict]]:
        """Group events into temporal clusters where related actions happen within CLUSTER_WINDOW_HOURS.

        Clustering strategy:
        1. Sliding window: events within CLUSTER_WINDOW_HOURS of each other form candidate groups.
        2. Entity overlap: groups sharing >= 1 entity are merged into a single cluster.
        3. Skill-sequence: groups with matching skill sets are merged.
        Result: list of clusters, each cluster is a list of event dicts.
        """
        if not events:
            return []

        window = timedelta(hours=CLUSTER_WINDOW_HOURS)

        # Step 1: build temporal groups (consecutive events within window)
        groups: list[list[dict]] = []
        current_group: list[dict] = [events[0]]
        for evt in events[1:]:
            if evt["ts"] - current_group[-1]["ts"] <= window:
                current_group.append(evt)
            else:
                if len(current_group) >= 1:
                    groups.append(current_group)
                current_group = [evt]
        if current_group:
            groups.append(current_group)

        # Step 2: merge groups that share entities or skill sets
        # Build adjacency and union-find style merge
        merged: list[list[dict]] = []
        used = [False] * len(groups)
        for i, g1 in enumerate(groups):
            if used[i]:
                continue
            combined = list(g1)
            e1 = self._extract_cluster_entities(g1)
            s1 = self._extract_cluster_skills(g1)
            for j, g2 in enumerate(groups[i + 1:], start=i + 1):
                if used[j]:
                    continue
                e2 = self._extract_cluster_entities(g2)
                s2 = self._extract_cluster_skills(g2)
                entity_overlap = len(e1 & e2) >= 1
                skill_overlap = len(s1 & s2) >= 1 and (s1 | s2) - {"web_search"}  # web_search alone not enough
                if entity_overlap or (skill_overlap and len(s1 | s2) >= 2):
                    combined.extend(g2)
                    used[j] = True
            merged.append(combined)
            used[i] = True

        # Step 3: only keep clusters with enough events to be a real pattern
        return [c for c in merged if len(c) >= CLUSTER_MIN_SIZE]

    def _extract_cluster_entities(self, cluster: list[dict]) -> set[str]:
        """All entities mentioned across all events in the cluster."""
        entities: set[str] = set()
        for evt in cluster:
            entities |= evt.get("entities", set())
        return entities

    def _extract_cluster_skills(self, cluster: list[dict]) -> set[str]:
        """All skills used across all events in the cluster."""
        skills: set[str] = set()
        for evt in cluster:
            skills |= evt.get("skills", set())
        return skills

    def _infer_intent(self, cluster: list[dict]) -> tuple[str, str]:
        """Rule-based intent inference from skill sequences.

        Returns (intent_id, workflow_label_es).
        Falls back to "monitoring" if no specific rule matches.
        """
        skills = self._extract_cluster_skills(cluster)
        for required, intent, label in _SEQUENCE_INTENTS:
            if required & skills == required:  # all required skills present
                return intent, label
        # Generic fallback
        return "monitoring", "monitoreo periódico"

    def _score_cluster(self, cluster: list[dict], intent: str) -> float:
        """Compute confidence score for a cluster.

        Score components:
        - Size: more events = higher confidence (capped)
        - Entity consistency: same entities repeated = higher
        - Skill consistency: same tools used repeatedly = higher
        - Recency: more recent events weight more

        Returns float 0.0–1.0.
        """
        size = len(cluster)

        # Size score: 2 events = 0.3, 3 = 0.5, 5+ = 0.7
        size_score = min(0.7, 0.3 + (size - 2) * 0.1)

        # Entity consistency: ratio of events that share the most common entity
        all_entities = self._extract_cluster_entities(cluster)
        if all_entities:
            entity_freq: dict[str, int] = defaultdict(int)
            for evt in cluster:
                for e in evt.get("entities", set()):
                    entity_freq[e] += 1
            max_freq = max(entity_freq.values())
            entity_score = min(0.2, (max_freq / size) * 0.2)
        else:
            entity_score = 0.0

        # Skill consistency: fraction of events that used at least one common skill
        all_skills = self._extract_cluster_skills(cluster)
        if all_skills:
            skill_freq: dict[str, int] = defaultdict(int)
            for evt in cluster:
                for s in evt.get("skills", set()):
                    skill_freq[s] += 1
            max_skill_freq = max(skill_freq.values()) if skill_freq else 0
            skill_score = min(0.1, (max_skill_freq / size) * 0.1)
        else:
            skill_score = 0.0

        # Apply threshold bonus: meeting v1 threshold (3+) in a cluster = +0.05
        threshold_bonus = 0.05 if size >= PATTERN_THRESHOLD else 0.0

        score = size_score + entity_score + skill_score + threshold_bonus
        return min(1.0, round(score, 3))

    def _build_smart_suggestion(
        self,
        cluster: list[dict],
        intent: str,
        workflow_label: str,
        entities: set[str],
        skills: set[str],
    ) -> str:
        """Build a specific, entity-aware automation suggestion.

        The suggestion names the detected entities and proposes a concrete workflow.
        Never generic — only fires when we have enough signal.
        """
        intent_label = _INTENT_LABELS.get(intent, "automatización")
        schedule = _INTENT_SCHEDULE.get(intent, "periódicamente")

        # Format entity list (crypto assets, domains, topics)
        crypto_entities = {e for e in entities if _CRYPTO_RE.search(e)}
        domain_entities = {e for e in entities if "." in e and not _CRYPTO_RE.search(e)}
        other_entities = entities - crypto_entities - domain_entities

        entity_parts: list[str] = []
        if crypto_entities:
            entity_parts.append(", ".join(e.upper() for e in sorted(crypto_entities)[:3]))
        if domain_entities:
            entity_parts.append(", ".join(sorted(domain_entities)[:2]))
        if other_entities and not entity_parts:
            entity_parts.append(", ".join(sorted(other_entities)[:3]))

        entity_str = " y ".join(entity_parts) if entity_parts else ""

        # Build subject line
        if entity_str:
            what_detected = f"estás haciendo {workflow_label} de {entity_str} varias veces"
        else:
            what_detected = f"estás haciendo {workflow_label} repetidamente"

        # Build action proposal based on intent + skills
        action_lines: list[str] = []
        if "gmail" in skills or intent == "reporting":
            action_lines.append("• Ejecutar el flujo automáticamente y enviarte el resultado por correo")
        if "browser" in skills or "fetch_url" in skills:
            if entity_str:
                action_lines.append(f"• Monitorear {entity_str} en segundo plano")
            else:
                action_lines.append("• Monitorear los recursos que te interesan en segundo plano")
        if "web_search" in skills and intent == "research":
            action_lines.append("• Buscar actualizaciones periódicamente y resumirlas")
        if not action_lines:
            # Generic fallback actions based on intent
            if intent == "monitoring":
                action_lines = [
                    f"• Monitorear {entity_str or 'el recurso'} cada {schedule}",
                    "• Notificarte cuando haya cambios importantes",
                ]
            elif intent == "reporting":
                action_lines = [
                    f"• Generar el reporte {schedule} automáticamente",
                    "• Enviarte el resultado sin que tengas que pedirlo",
                ]
            else:
                action_lines = [
                    f"• Ejecutar esta tarea {schedule}",
                    "• Notificarte con los resultados",
                ]

        actions_text = "\n".join(action_lines)

        suggestion = (
            f"Noté que {what_detected}.\n\n"
            f"Puedo automatizar este flujo de {intent_label} para ti:\n"
            f"{actions_text}\n\n"
            f"¿Quieres que configure esto para que corra {schedule} automáticamente?"
        )
        return suggestion

    def _cluster_fingerprint(self, intent: str, entities: set[str]) -> str:
        """Stable fingerprint for a pattern (intent + top entities). Used for 7-day dedup."""
        top_entities = sorted(entities)[:4]
        raw = f"{intent}:{':'.join(top_entities)}"
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    # ──────────────────────────────────────────────────────────────────────
    # V2 — Pattern Memory (Redis, Phase 6)
    # ──────────────────────────────────────────────────────────────────────

    async def _load_known_patterns(self) -> set[str]:
        """Load fingerprints of previously discovered patterns (7-day window)."""
        if not self.redis_url:
            return set()
        try:
            r = aioredis.from_url(self.redis_url, decode_responses=True)
            try:
                keys = await r.keys("opp:pattern:*")
                return {k.split("opp:pattern:")[1] for k in keys if "opp:pattern:" in k}
            finally:
                await r.aclose()
        except Exception:
            return set()

    async def _save_pattern_memory(self, fingerprint: str, intent: str, entities: set[str]) -> None:
        """Persist a discovered pattern so it's not re-suggested for 7 days."""
        if not self.redis_url:
            return
        try:
            r = aioredis.from_url(self.redis_url, decode_responses=True)
            try:
                key = f"opp:pattern:{fingerprint}"
                value = json.dumps({
                    "intent": intent,
                    "entities": sorted(entities)[:5],
                    "discovered_at": datetime.now(timezone.utc).isoformat(),
                })
                await r.set(key, value, ex=604800)  # 7 days
            finally:
                await r.aclose()
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────────────────
    # Suggestion Delivery (unchanged)
    # ──────────────────────────────────────────────────────────────────────

    async def _send_suggestion(self, opp: dict) -> None:
        """Publish suggestion as Telegram message."""
        text = opp.get("suggestion", "")
        if not text:
            return
        try:
            await safe_notify(
                self.bus,
                str(self.notify_chat_id),
                text,
                source="opportunity_engine",
            )
        except Exception as exc:
            logger.debug("opportunity_engine.send_failed", error=str(exc)[:120])

    # ──────────────────────────────────────────────────────────────────────
    # Redis Rate Limiting & Dedup (unchanged)
    # ──────────────────────────────────────────────────────────────────────

    def _today_key(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%d")

    def _day_offset_key(self, hours: int = 48) -> str:
        return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y%m%d")

    async def _is_rate_limited(self, user_id: str) -> bool:
        if not self.redis_url:
            return False
        try:
            r = aioredis.from_url(self.redis_url, decode_responses=True)
            try:
                key = f"opp:daily:{user_id}:{self._today_key()}"
                count_raw = await r.get(key)
                count = int(count_raw) if count_raw else 0
                return count >= MAX_SUGGESTIONS_PER_DAY
            finally:
                await r.aclose()
        except Exception:
            return False

    async def _increment_daily_count(self, user_id: str) -> None:
        if not self.redis_url:
            return
        try:
            r = aioredis.from_url(self.redis_url, decode_responses=True)
            try:
                key = f"opp:daily:{user_id}:{self._today_key()}"
                pipe = r.pipeline()
                pipe.incr(key)
                pipe.expire(key, 86400)
                await pipe.execute()
            finally:
                await r.aclose()
        except Exception:
            pass

    async def _was_suggested_recently(self, user_id: str, opp_type: str) -> bool:
        if not self.redis_url:
            return False
        try:
            r = aioredis.from_url(self.redis_url, decode_responses=True)
            try:
                today = self._today_key()
                # Check today, yesterday, and 2 days ago to cover the full 48h window
                for day in (today, self._day_offset_key(24), self._day_offset_key(48)):
                    key = f"opp:suggested:{user_id}:{opp_type}:{day}"
                    if await r.exists(key):
                        return True
                return False
            finally:
                await r.aclose()
        except Exception:
            return False

    async def _mark_suggested(self, user_id: str, opp_type: str) -> None:
        if not self.redis_url:
            return
        try:
            r = aioredis.from_url(self.redis_url, decode_responses=True)
            try:
                key = f"opp:suggested:{user_id}:{opp_type}:{self._today_key()}"
                await r.set(key, "1", ex=DEDUP_WINDOW_HOURS * 3600)
            finally:
                await r.aclose()
        except Exception:
            pass

    async def _enqueue_opportunity(self, user_id: str, opp: dict) -> None:
        if not self.redis_url:
            return
        try:
            r = aioredis.from_url(self.redis_url, decode_responses=True)
            try:
                key = f"opp:queue:{user_id}"
                existing_raw = await r.get(key)
                queue: list = json.loads(existing_raw) if existing_raw else []
                if not any(q["type"] == opp["type"] for q in queue):
                    queue.append(opp)
                await r.set(key, json.dumps(queue), ex=86400)
            finally:
                await r.aclose()
        except Exception:
            pass

    async def _dequeue_opportunity(self, user_id: str) -> dict | None:
        if not self.redis_url:
            return None
        try:
            r = aioredis.from_url(self.redis_url, decode_responses=True)
            try:
                key = f"opp:queue:{user_id}"
                raw = await r.get(key)
                if not raw:
                    return None
                queue: list = json.loads(raw)
                if not queue:
                    return None
                item = queue.pop(0)
                if queue:
                    await r.set(key, json.dumps(queue), ex=86400)
                else:
                    await r.delete(key)
                return item
            finally:
                await r.aclose()
        except Exception:
            return None
