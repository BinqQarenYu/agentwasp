"""Persistent Knowledge Graph — structured world model for Agent Wasp.

Stores entities (people, places, concepts, preferences, assets) and their
relationships as a graph. Unlike episodic memory (raw experiences), the KG
contains distilled, structured facts that persist indefinitely.

Storage:
  - PostgreSQL: KnowledgeNode + KnowledgeRelation tables (durable, queryable)
  - Redis: kg:entity:{id} HASH (fast reads for context injection)

Auto-extraction:
  - After each conversation, extract_from_conversation() runs a lightweight
    LLM call to detect new entities and relationships.
  - Existing nodes are updated (confidence increased), not duplicated.

Usage in context:
  - format_for_context() returns a compact summary for injection into LLM prompt.
"""

import json
import re
import structlog
from uuid import uuid4
from datetime import datetime, timezone

import redis.asyncio as aioredis
from sqlalchemy import select, or_

from ..db.models import KnowledgeNode, KnowledgeRelation
from ..db.session import async_session

logger = structlog.get_logger()

KG_NODE_PREFIX = "kg:node:"
KG_INDEX_KEY = "kg:index"          # HASH: name_lower → node_id
KG_ENTITY_TYPES = {
    "person", "place", "concept", "preference", "fact",
    "organization", "asset", "skill", "event", "time",
}

# Patterns for fast rule-based extraction (no LLM needed for common cases).
# Captures are bounded to short noun phrases (≤ 50 chars, stops at conjunctions
# and clause separators) to avoid greedy "rest of sentence" entities.
_NP_BOUNDARY = r"(?:\.|,|;|\?|!|$|\sy\s|\so\s|\sand\s|\sor\s|\spara\s|\sporque\s|\spero\s|\sthen\s|\sso\s)"
_PREFERENCE_PATTERNS = [
    # Spanish preferences — short noun phrase only.
    # Synonyms added: "uso" → "uso|ocupo|manejo|trabajo con" (extends existing pattern; no new types).
    (re.compile(rf"\b(?:prefiero|me gusta(?:n)?|me encanta(?:n)?|uso|utilizo|ocupo|manejo)\s+([\w][\w\s\-./]{{1,48}}?)\s*{_NP_BOUNDARY}", re.I), "prefers"),
    (re.compile(rf"\b(?:no me gusta(?:n)?|odio|detesto|no uso)\s+([\w][\w\s\-./]{{1,48}}?)\s*{_NP_BOUNDARY}", re.I), "dislikes"),
    (re.compile(rf"\b(?:mi\s+(?:fuente|portal|sitio)\s+(?:favorito|preferido|de confianza))\s+(?:es|para.+?es)\s+([\w][\w\s\-./]{{1,48}}?)\s*{_NP_BOUNDARY}", re.I), "preferred_source"),
    (re.compile(rf"\b(?:trabajo\s+con|estoy\s+usando|estamos?\s+usando)\s+([\w][\w\s\-./]{{1,48}}?)\s*{_NP_BOUNDARY}", re.I), "uses"),
    (re.compile(rf"\b(?:mi\s+(?:proyecto|app|aplicaci[oó]n|sistema|bot|script))\s+(?:se\s+llama|es)\s+([\w][\w\s\-./]{{1,40}}?)\s*{_NP_BOUNDARY}", re.I), "project_name"),
    # Implicit infra/hosting (Spanish). Synonyms added: "corre en" → "corre en|está en|lo tengo en|deployado en" (extends existing pattern; no new types).
    (re.compile(rf"\b(?:lo\s+(?:monto|corro|tengo)|corre|está\s+(?:en|alojad[oa]|desplegad[oa])|deployé|deploy[eé]|deployad[oa]\s+en|lo\s+tengo\s+en)\s+(?:en|sobre)?\s*([\w][\w\s\-./]{{1,40}}?)\s*{_NP_BOUNDARY}", re.I), "hosted_on"),
    # Implicit project/asset ownership. Synonyms added: "tengo" → "tengo|hice|armé|monté" (extends existing pattern; no new types).
    (re.compile(rf"\b(?:tengo|hice|armé|monté|levanté)\s+(?:un|una)\s+(?:proyecto|app|aplicaci[oó]n|api|sistema|bot|servidor|cluster|pipeline|database|servicio)\s+(?:en|con|usando|sobre)\s+([\w][\w\s\-./]{{1,40}}?)\s*{_NP_BOUNDARY}", re.I), "uses"),
    # English preferences — short noun phrase only
    (re.compile(rf"\b(?:I\s+(?:use|prefer|like|love|am\s+using|work\s+with|rely\s+on))\s+([\w][\w\s\-./]{{1,48}}?)\s*{_NP_BOUNDARY}", re.I), "prefers"),
    (re.compile(rf"\b(?:I\s+(?:don't\s+(?:use|like)|hate|dislike|avoid))\s+([\w][\w\s\-./]{{1,48}}?)\s*{_NP_BOUNDARY}", re.I), "dislikes"),
    (re.compile(rf"\b(?:my\s+(?:favorite|preferred|go-to|main))\s+(?:tool|language|framework|library|platform)\s+is\s+([\w][\w\s\-./]{{1,48}}?)\s*{_NP_BOUNDARY}", re.I), "prefers"),
    (re.compile(rf"\b(?:I(?:'m|\s+am)\s+working\s+(?:on|with))\s+([\w][\w\s\-./]{{1,48}}?)\s*{_NP_BOUNDARY}", re.I), "uses"),
    # NEW — English implicit infra: "deployed on Vercel", "running on AWS", "hosted on Railway"
    (re.compile(rf"\b(?:deployed|running|hosted|deployed)\s+on\s+([\w][\w\s\-./]{{1,40}}?)\s*{_NP_BOUNDARY}", re.I), "hosted_on"),
]

_PERSON_PATTERNS = [
    re.compile(r"\b(?:mi\s+(?:hermano|hermana|padre|madre|amigo|amiga|jefe|esposo|esposa|pareja|hijo|hija))\s+(?:se llama|es)\s+(\w+)", re.I),
    re.compile(r"\b(\w+)\s+(?:es\s+mi\s+(?:hermano|hermana|padre|madre|amigo|amiga|jefe|esposo|esposa|pareja))", re.I),
    re.compile(r"\b(?:my\s+(?:brother|sister|father|mother|friend|boss|partner|son|daughter))\s+(?:is\s+called|is|named)\s+(\w+)", re.I),
    re.compile(r"\b(\w+)\s+(?:is\s+my\s+(?:brother|sister|father|mother|friend|boss|partner))", re.I),
]

_SOURCE_PATTERNS = re.compile(
    r"\b(?:en|desde|usa|usa|using|from|via|through)\s+"
    r"(Binance|CoinGecko|CoinMarketCap|TradingView|Yahoo\s+Finance|Bloomberg|Reuters|"
    r"Bitstamp|Kraken|Bybit|Coinbase|Bitfinex|OKX|Huobi|Gate\.io)\b",
    re.I,
)

# Concepts and tools mentioned by name — capitalized words that look like product/tool names
# Matches: Python, JavaScript, Docker, FastAPI, PostgreSQL, Redis, etc.
# Conservative: only well-known brand/product names. Generic words excluded.
_TECH_TOOL_PATTERN = re.compile(
    r"\b("
    # Languages
    r"Python|JavaScript|TypeScript|Node\.?js|Java|Kotlin|Swift|Rust|Golang|"
    # Frameworks
    r"React|Vue|Angular|Svelte|FastAPI|Django|Flask|Express|Next\.?js|Nuxt|Remix|"
    # Containers / infra
    r"Docker|Kubernetes|Podman|Helm|Terraform|Ansible|Jenkins|"
    # Databases
    r"PostgreSQL|MySQL|SQLite|MongoDB|Redis|Elasticsearch|Cassandra|DynamoDB|Snowflake|BigQuery|Supabase|Firebase|"
    # ML / data
    r"TensorFlow|PyTorch|scikit-learn|Pandas|NumPy|Jupyter|Celery|RabbitMQ|Kafka|Spark|Airflow|dbt|"
    # Source control / CI
    r"GitHub|GitLab|Bitbucket|"
    # Cloud
    r"AWS|GCP|Azure|Heroku|Vercel|Netlify|Cloudflare|Railway|Fly\.io|DigitalOcean|"
    # Web servers / protocols
    r"Nginx|Apache|GraphQL|gRPC|"
    # Comms / SaaS
    r"Telegram|WhatsApp|Slack|Discord|Notion|Linear|Jira|Trello|"
    r"Stripe|PayPal|Twilio|SendGrid|"
    # AI providers / models / tools
    r"OpenAI|Anthropic|Claude|GPT|"
    r"LangChain|LlamaIndex|ChromaDB|Pinecone|Weaviate|Qdrant|Ollama|Mistral|Gemini|Cohere|"
    # Automation
    r"Zapier|Make|n8n"
    r")\b",
    # Note: LLaMA / REST excluded — false-matches Spanish verb "llama/llamar" /
    # "rest" too generic in English.
    re.I,
)


async def _get_redis(redis_url: str):
    return aioredis.from_url(redis_url, decode_responses=True)


# ──────────────────────────────────────────────────────────────────────────
# Light alias normalization — collapses semantically identical entity names
# to a single canonical form before insertion. Exact-match dictionary only,
# capped at ≤ 10 entries. No fuzzy matching, no inference.
# ──────────────────────────────────────────────────────────────────────────
_KG_CANONICAL_ALIASES: dict[str, str] = {
    "cloudflare workers":   "Cloudflare",
    "cf workers":           "Cloudflare",
    "amazon web services":  "AWS",
    "google cloud":         "GCP",
    "google cloud platform": "GCP",
    "microsoft azure":      "Azure",
    "node":                 "Node.js",
    "nodejs":               "Node.js",
    "postgres":             "PostgreSQL",
    "psql":                 "PostgreSQL",
}


def _canonicalize_entity_name(name: str) -> str:
    """Return the canonical form for known semantic duplicates, else the input."""
    if not name:
        return name
    return _KG_CANONICAL_ALIASES.get(name.strip().lower(), name)


async def get_or_create_node(
    name: str,
    entity_type: str = "entity",
    description: str = "",
    source_chat_id: str = "",
    redis_url: str = "",
    metadata: dict | None = None,
) -> str:
    """Get existing node by name or create a new one. Returns node_id."""
    # Section 4 — collapse known semantic aliases to canonical form first.
    name_clean = _canonicalize_entity_name(name.strip())
    if not name_clean:
        return ""

    try:
        async with async_session() as session:
            # Check if node already exists
            result = await session.execute(
                select(KnowledgeNode).where(
                    KnowledgeNode.name.ilike(name_clean)
                ).limit(1)
            )
            existing = result.scalar_one_or_none()
            if existing:
                # Update confidence and description if provided
                if description and not existing.description:
                    existing.description = description
                existing.confidence = min(1.0, existing.confidence + 0.1)
                existing.updated_at = datetime.now(timezone.utc)
                await session.commit()
                return existing.id

            # Create new node
            node_id = str(uuid4())
            node = KnowledgeNode(
                id=node_id,
                name=name_clean,
                entity_type=entity_type,
                description=description,
                source_chat_id=source_chat_id,
                metadata_json=metadata or {},
            )
            session.add(node)
            await session.commit()

        # Cache in Redis
        if redis_url:
            r = await _get_redis(redis_url)
            try:
                await r.hset(f"{KG_NODE_PREFIX}{node_id}", mapping={
                    "name": name_clean,
                    "type": entity_type,
                    "description": description,
                })
                await r.hset(KG_INDEX_KEY, name_clean.lower(), node_id)
            finally:
                await r.aclose()

        logger.info("kg.node_created", name=name_clean, type=entity_type)
        return node_id

    except Exception:
        logger.exception("kg.node_error", name=name_clean)
        return ""


async def add_relation(
    from_name: str,
    relation_type: str,
    to_name_or_value: str,
    source_chat_id: str = "",
    redis_url: str = "",
) -> bool:
    """Add a relationship between two entities (or entity → literal value)."""
    if not from_name or not relation_type or not to_name_or_value:
        return False

    try:
        from_id = await get_or_create_node(from_name, source_chat_id=source_chat_id, redis_url=redis_url)
        if not from_id:
            return False

        # to_name might be a literal value (e.g. "CoinGecko", "March 15") or another entity
        to_id = await get_or_create_node(to_name_or_value, source_chat_id=source_chat_id, redis_url=redis_url)

        async with async_session() as session:
            # Check for duplicate
            existing = await session.execute(
                select(KnowledgeRelation).where(
                    KnowledgeRelation.from_node_id == from_id,
                    KnowledgeRelation.relation_type == relation_type,
                    KnowledgeRelation.to_node_id == to_id,
                ).limit(1)
            )
            if existing.scalar_one_or_none():
                return True  # Already exists

            rel = KnowledgeRelation(
                id=str(uuid4()),
                from_node_id=from_id,
                to_node_id=to_id,
                relation_type=relation_type,
                value=to_name_or_value,
            )
            session.add(rel)
            await session.commit()

        logger.info("kg.relation_added", from_=from_name, rel=relation_type, to=to_name_or_value)
        # Section 5 — lightweight fire log: entity + type only, no payload.
        logger.info("kg.fire", entity=to_name_or_value[:40], type=relation_type)
        return True
    except Exception:
        logger.exception("kg.relation_error")
        return False


async def extract_from_conversation(
    user_input: str,
    agent_response: str,
    chat_id: str = "",
    redis_url: str = "",
) -> int:
    """Rule-based extraction of entities and relations from a conversation turn.
    Returns number of new facts extracted."""
    extracted = 0
    candidates_checked = 0
    user_entity = "Usuario"

    logger.debug(
        "kg.extract_start",
        input_len=len(user_input),
        chat_id=chat_id,
    )

    # 1. Preference/usage extraction (Spanish + English)
    for pattern, rel_type in _PREFERENCE_PATTERNS:
        for m in pattern.finditer(user_input):
            target = m.group(1).strip().rstrip(".,!? ")
            # Allow short tool names (min 2 chars) but not single words over 60 chars
            candidates_checked += 1
            if 2 <= len(target) <= 60:
                ok = await add_relation(user_entity, rel_type, target, chat_id, redis_url)
                if ok:
                    extracted += 1
            else:
                logger.debug("kg.candidate_rejected", reason="length", value=target[:40])

    # 2. Person extraction
    for pattern in _PERSON_PATTERNS:
        for m in pattern.finditer(user_input):
            name = m.group(1).strip()
            candidates_checked += 1
            if name and 2 <= len(name) <= 50:
                await get_or_create_node(name, "person", source_chat_id=chat_id, redis_url=redis_url)
                extracted += 1

    # 3. Source preference extraction (crypto/finance platforms)
    for m in _SOURCE_PATTERNS.finditer(user_input):
        source = m.group(1).strip()
        candidates_checked += 1
        ok = await add_relation(user_entity, "preferred_source", source, chat_id, redis_url)
        if ok:
            extracted += 1

    # 4. Tech tool / concept mentions — any recognized tool name in the conversation
    #    is recorded as a concept node so the KG grows with every tech discussion.
    seen_tools: set[str] = set()
    for m in _TECH_TOOL_PATTERN.finditer(user_input + " " + agent_response):
        tool = m.group(1)
        tool_key = tool.lower()
        if tool_key not in seen_tools:
            seen_tools.add(tool_key)
            candidates_checked += 1
            ok = await add_relation(user_entity, "uses_tool", tool, chat_id, redis_url)
            if ok:
                extracted += 1

    logger.debug(
        "kg.extract_done",
        candidates_checked=candidates_checked,
        extracted=extracted,
        chat_id=chat_id,
    )

    return extracted


async def search_nodes(keyword: str, limit: int = 10) -> list[dict]:
    """Search for nodes by name or description."""
    try:
        async with async_session() as session:
            result = await session.execute(
                select(KnowledgeNode).where(
                    or_(
                        KnowledgeNode.name.ilike(f"%{keyword}%"),
                        KnowledgeNode.description.ilike(f"%{keyword}%"),
                    )
                ).order_by(KnowledgeNode.confidence.desc()).limit(limit)
            )
            nodes = result.scalars().all()
            return [
                {
                    "id": n.id,
                    "name": n.name,
                    "type": n.entity_type,
                    "description": n.description,
                    "confidence": n.confidence,
                }
                for n in nodes
            ]
    except Exception:
        logger.exception("kg.search_error")
        return []


async def get_relations_for(node_name: str, limit: int = 20) -> list[dict]:
    """Get all known relations for a named entity."""
    try:
        async with async_session() as session:
            node_result = await session.execute(
                select(KnowledgeNode).where(KnowledgeNode.name.ilike(node_name)).limit(1)
            )
            node = node_result.scalar_one_or_none()
            if not node:
                return []

            rel_result = await session.execute(
                select(KnowledgeRelation, KnowledgeNode).join(
                    KnowledgeNode, KnowledgeRelation.to_node_id == KnowledgeNode.id
                ).where(KnowledgeRelation.from_node_id == node.id).limit(limit)
            )
            return [
                {
                    "relation": row[0].relation_type,
                    "target": row[1].name,
                    "value": row[0].value,
                }
                for row in rel_result.all()
            ]
    except Exception:
        logger.exception("kg.get_relations_error")
        return []


async def get_user_profile(chat_id: str = "", limit: int = 30) -> list[dict]:
    """Get all facts about the user (entity='Usuario')."""
    return await get_relations_for("Usuario", limit=limit)


async def format_for_context(chat_id: str = "", max_facts: int = 15) -> str:
    """Format knowledge graph facts as a compact block for LLM context injection."""
    try:
        facts = await get_user_profile(chat_id, limit=max_facts)
        if not facts:
            return ""

        lines = ["[WHAT I KNOW ABOUT YOU:]"]
        for f in facts:
            rel = f["relation"].replace("_", " ")
            target = f["target"]
            lines.append(f"  • {rel}: {target}")

        return "\n".join(lines)
    except Exception:
        return ""


async def get_node_count() -> int:
    """Return total number of knowledge graph nodes."""
    try:
        from sqlalchemy import func
        async with async_session() as session:
            result = await session.execute(select(func.count(KnowledgeNode.id)))
            return result.scalar_one() or 0
    except Exception:
        return 0


# ── Entity normalization helpers (MA1 / MA3 / MA5) ──────────────────────────

# Trailing noise phrases stripped before splitting
_NOISE_SUFFIX_RE = re.compile(
    r"\s+(?:for\s+(?:my\s+)?(?:project|this|that|app|work|bot|system|setup)|"
    r"setup|integration|stack|pipeline|backend|frontend|service|solution)\s*$",
    re.I,
)
# Separators that indicate a compound entity string
_COMPOUND_SEP_RE = re.compile(
    r"\s+and\s+|,\s*|\s+with\s+|\s+over\s+|\s+or\s+",
    re.I
)


def _split_and_normalize_entity(name: str) -> list[dict]:
    """Split compound entity names and strip noise suffixes.

    Returns a list of {"name": str, "from_split": bool} dicts.
    "Python and Docker for my project" → [{"name":"Python","from_split":True}, {"name":"Docker","from_split":True}]
    "PostgreSQL" → [{"name":"PostgreSQL","from_split":False}]
    """
    cleaned = _NOISE_SUFFIX_RE.sub("", name.strip())
    parts = _COMPOUND_SEP_RE.split(cleaned)
    tokens = [p.strip() for p in parts if len(p.strip()) > 2]
    if len(tokens) > 1:
        return [{"name": t, "from_split": True} for t in tokens]
    single = tokens[0] if tokens else name.strip()
    return [{"name": single, "from_split": False}]


def _normalize_for_tool_hint(name: str) -> str:
    """Lowercase, strip, keep only alphanumeric + dot for _TOOL_SKILL_HINT lookup.

    "Python " → "python", "Node.js" → "node.js", "PostgreSQL" → "postgresql"
    """
    return re.sub(r"[^a-z0-9.]", "", name.lower().strip())


# ── Salience scoring ────────────────────────────────────────────────────────

def _compute_salience(
    confidence: float, updated_at: datetime, recency_factor: float = 1.0
) -> float:
    """Combine confidence (mention frequency) and recency into a 0–1 salience score.

    confidence:     0–1+ float that grows each time the entity is re-mentioned.
    recency:        decays from 1.0 toward ~0.5 as the entity ages past ~3 days.
    recency_factor: multiplier applied to the recency term (0.9 for split-derived entities).
    """
    ua = updated_at if updated_at.tzinfo else updated_at.replace(tzinfo=timezone.utc)
    days_old = max(0.0, (datetime.now(timezone.utc) - ua).total_seconds() / 86400.0)
    recency = 1.0 / (1.0 + days_old * 0.3)
    return round(min(1.0, confidence) * 0.6 + recency * recency_factor * 0.4, 4)


# ── Tool → recommended skill mapping ────────────────────────────────────────

_TOOL_SKILL_HINT: dict[str, str] = {
    "python":      "python_exec",
    "javascript":  "python_exec",
    "typescript":  "python_exec",
    "docker":      "shell",
    "kubernetes":  "shell",
    "postgresql":  "python_exec",
    "mysql":       "python_exec",
    "mongodb":     "python_exec",
    "redis":       "python_exec",
    "fastapi":     "python_exec",
    "django":      "python_exec",
    "flask":       "python_exec",
    "react":       "python_exec",
    "node.js":     "python_exec",
    "nodejs":      "python_exec",
    "github":      "shell",
    "gitlab":      "shell",
    "aws":         "shell",
    "gcp":         "shell",
    "azure":       "shell",
    "terraform":   "shell",
    "nginx":       "shell",
    "tensorflow":  "python_exec",
    "pytorch":     "python_exec",
    "pandas":      "python_exec",
    "numpy":       "python_exec",
    "openai":      "http_request",
    "anthropic":   "http_request",
    "langchain":   "python_exec",
    "llamaindex":  "python_exec",
    "chromadb":    "python_exec",
    "pinecone":    "python_exec",
    "ollama":      "http_request",
}

# Relation types considered as "tool usage" for biasing
_TOOL_RELATION_TYPES = frozenset({"uses_tool", "prefers", "uses"})


async def get_salient_entities(
    intent: str = "",
    chat_id: str = "",
    limit: int = 5,
) -> list[dict]:
    """Fetch entities related to the user sorted by salience score.

    Returns dicts with keys: name, relation_type, salience, confidence, updated_at.
    If intent is provided, entities whose names appear in the intent text are boosted.
    """
    try:
        async with async_session() as session:
            # Find the "Usuario" anchor node
            res = await session.execute(
                select(KnowledgeNode).where(KnowledgeNode.name.ilike("Usuario")).limit(1)
            )
            usuario = res.scalar_one_or_none()
            if not usuario:
                return []

            # Fetch all relations from Usuario + join target node
            rel_res = await session.execute(
                select(KnowledgeRelation, KnowledgeNode).join(
                    KnowledgeNode, KnowledgeRelation.to_node_id == KnowledgeNode.id
                ).where(KnowledgeRelation.from_node_id == usuario.id)
            )
            rows = rel_res.all()

        if not rows:
            return []

        intent_lower = intent.lower()
        # MA5: dedup by normalized name — keep highest-salience entry per token
        seen: dict[str, dict] = {}

        for rel, node in rows:
            # MA1: expand compound entity names into structured items
            items = _split_and_normalize_entity(node.name)
            for item in items:
                token = item["name"]
                # Split-derived entities get dampened recency to prevent salience inflation
                recency_factor = 0.9 if item["from_split"] else 1.0
                sal = _compute_salience(node.confidence, node.updated_at, recency_factor=recency_factor)
                # MA2: soft intent boost (multiplier instead of hard override)
                if intent_lower and token.lower() in intent_lower:
                    sal = min(1.0, sal * 1.25)
                norm_key = token.lower().strip()  # MA5: dedup key
                if norm_key not in seen or sal > seen[norm_key]["salience"]:
                    seen[norm_key] = {
                        "name":          token,
                        "relation_type": rel.relation_type,
                        "salience":      round(sal, 4),
                        "confidence":    node.confidence,
                        "updated_at":    node.updated_at,
                    }

        scored = sorted(seen.values(), key=lambda x: x["salience"], reverse=True)
        return scored[:limit]

    except Exception:
        logger.exception("kg.get_salient_error")
        return []


def _salience_label(salience: float, confidence: float) -> str:
    """Human-readable frequency/recency label for an entity."""
    if confidence >= 1.3 or salience >= 0.85:
        return "frecuente"
    if confidence >= 1.1 or salience >= 0.70:
        return "habitual"
    return "reciente"


async def format_salient_for_context(
    chat_id: str = "",
    intent: str = "",
    max_entities: int = 3,
) -> str:
    """Intent-aware KG context block for the system prompt.

    Phase B improvements over format_for_context():
    - Entities ranked by salience (frequency × recency) not insertion order
    - Only top max_entities returned (signal, not noise)
    - Entities matching the current intent are boosted to the top
    - Tool-bias hint line: recommends skills based on user's known tech stack
    - Opportunity summary: shows insight count so the agent knows KG is populated
    """
    try:
        entities = await get_salient_entities(intent=intent, chat_id=chat_id, limit=max_entities + 5)
        if not entities:
            return ""

        # Split into tool-type and non-tool entities
        tool_entities = [e for e in entities if e["relation_type"] in _TOOL_RELATION_TYPES]
        all_top = entities[:max_entities]

        # Build entity line
        entity_parts = []
        for e in all_top:
            label = _salience_label(e["salience"], e["confidence"])
            entity_parts.append(f"{e['name']} ({label})")

        lines = ["[WHAT I KNOW ABOUT YOU:]"]
        lines.append("Technologies/preferences: " + ", ".join(entity_parts))

        # Build tool-bias hint if we have recognized tools
        # MA3: normalize entity name before dict lookup
        skill_hints: list[str] = []
        seen_skills: set[str] = set()
        for e in tool_entities[:6]:
            skill = _TOOL_SKILL_HINT.get(_normalize_for_tool_hint(e["name"]))
            if skill and skill not in seen_skills:
                seen_skills.add(skill)
                skill_hints.append(skill)

        if skill_hints:
            lines.append("Preferred skills for this user: " + " · ".join(skill_hints))

        return "\n".join(lines)

    except Exception:
        logger.exception("kg.format_salient_error")
        return ""


# ── KG Insights (Step 5 — Opportunity Signal Feed) ──────────────────────────

KG_INSIGHTS_KEY = "kg:insights"


async def compute_and_store_kg_insights(
    chat_id: str = "",
    redis_url: str = "",
) -> dict:
    """Compute KG usage patterns and store as a Redis insight snapshot.

    Stored at Redis key ``kg:insights`` (TTL 3 hours).
    Returns the insights dict (empty on error).
    """
    try:
        from sqlalchemy import func as _func

        async with async_session() as session:
            # Total counts
            node_count = (await session.execute(
                select(_func.count(KnowledgeNode.id))
            )).scalar() or 0

            rel_count = (await session.execute(
                select(_func.count(KnowledgeRelation.id))
            )).scalar() or 0

            # Top tools: nodes related to Usuario via tool-relation types
            usuario_res = await session.execute(
                select(KnowledgeNode).where(KnowledgeNode.name.ilike("Usuario")).limit(1)
            )
            usuario = usuario_res.scalar_one_or_none()

            top_tools: list[str] = []
            # MA4: collect raw rows to compute dominant_stack + recent_focus
            _all_entity_rows: list[tuple] = []

            if usuario:
                tool_res = await session.execute(
                    select(KnowledgeRelation, KnowledgeNode).join(
                        KnowledgeNode, KnowledgeRelation.to_node_id == KnowledgeNode.id
                    ).where(
                        KnowledgeRelation.from_node_id == usuario.id,
                        KnowledgeRelation.relation_type.in_(list(_TOOL_RELATION_TYPES)),
                    ).order_by(KnowledgeNode.confidence.desc()).limit(10)
                )
                for _rel, _node in tool_res.all():
                    # MA1: expand compound tool names (use item["name"] from structured return)
                    for item in _split_and_normalize_entity(_node.name):
                        norm = item["name"].lower().strip()
                        if norm not in {t.lower() for t in top_tools}:
                            top_tools.append(item["name"])

                all_res = await session.execute(
                    select(KnowledgeRelation, KnowledgeNode).join(
                        KnowledgeNode, KnowledgeRelation.to_node_id == KnowledgeNode.id
                    ).where(
                        KnowledgeRelation.from_node_id == usuario.id,
                    ).order_by(KnowledgeNode.confidence.desc()).limit(15)
                )
                _all_entity_rows = all_res.all()

        # MA4: build derived insight fields from already-fetched rows (no extra DB queries)
        top_entities: list[str] = []
        _ent_salience: list[dict] = []
        _seen_norm: set[str] = set()

        for _rel, _node in _all_entity_rows:
            ua = _node.updated_at if _node.updated_at.tzinfo else _node.updated_at.replace(tzinfo=timezone.utc)
            sal = _compute_salience(_node.confidence, ua)
            for item in _split_and_normalize_entity(_node.name):
                norm = item["name"].lower().strip()
                if norm not in _seen_norm:
                    _seen_norm.add(norm)
                    top_entities.append(item["name"])
                    _ent_salience.append({"name": item["name"], "salience": sal, "updated_at": ua})

        # dominant_stack: top 3 by salience score
        dominant_stack = [
            e["name"] for e in sorted(_ent_salience, key=lambda x: x["salience"], reverse=True)[:3]
        ]
        # recent_focus: top 3 by most recently updated
        recent_focus = [
            e["name"] for e in sorted(_ent_salience, key=lambda x: x["updated_at"], reverse=True)[:3]
        ]

        insights = {
            "node_count":      node_count,
            "rel_count":       rel_count,
            "top_tools":       top_tools[:5],
            "top_entities":    top_entities[:5],
            "dominant_stack":  dominant_stack,
            "recent_focus":    recent_focus,
            "updated_at":      datetime.now(timezone.utc).isoformat(),
        }

        if redis_url:
            r = await _get_redis(redis_url)
            try:
                key = f"{KG_INSIGHTS_KEY}:{chat_id}" if chat_id else KG_INSIGHTS_KEY
                await r.set(key, json.dumps(insights), ex=10800)  # TTL 3h
            finally:
                await r.aclose()

        logger.info(
            "kg.insights_updated",
            nodes=node_count,
            rels=rel_count,
            top_tools=top_tools[:3],
        )
        return insights

    except Exception:
        logger.exception("kg.insights_error")
        return {}
