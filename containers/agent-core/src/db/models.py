from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class MemoryEntry(Base):
    __tablename__ = "memory_entries"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    memory_type: Mapped[str] = mapped_column(String(50), index=True)
    project_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), nullable=True, index=True)
    file_path: Mapped[str] = mapped_column(String(500), unique=True)
    tags: Mapped[list] = mapped_column(ARRAY(String), default=list)
    content_summary: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    version: Mapped[int] = mapped_column(Integer, default=1)


class MemorySnapshot(Base):
    __tablename__ = "memory_snapshots"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    label: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    entry_count: Mapped[int] = mapped_column(Integer, default=0)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    trigger: Mapped[str] = mapped_column(String(50), default="manual")
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        # Composite index for chat-scoped queries (dashboard pagination, per-chat audit)
        Index("ix_audit_log_chat_id_timestamp", "chat_id", "timestamp"),
    )

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    source: Mapped[str] = mapped_column(String(50), default="")
    action: Mapped[str] = mapped_column(String(200), default="")
    input_summary: Mapped[str] = mapped_column(Text, default="")
    output_summary: Mapped[str] = mapped_column(Text, default="")
    user_id: Mapped[str] = mapped_column(String(50), default="")
    chat_id: Mapped[str] = mapped_column(String(50), default="")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class AgentRecord(Base):
    """Persistent agent registry — mirrors Redis HASH 'agents' for queryability."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(50), default="idle", index=True)
    model_provider: Mapped[str] = mapped_column(String(100), default="")
    model_name: Mapped[str] = mapped_column(String(200), default="")
    memory_namespace: Mapped[str] = mapped_column(String(200), default="")
    autonomy_mode: Mapped[str] = mapped_column(String(50), default="semi")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class AgentMessage(Base):
    """Agent-to-agent message bus — persistent and queryable."""

    __tablename__ = "agent_messages"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    from_agent_id: Mapped[str] = mapped_column(String(200), index=True)
    to_agent_id: Mapped[str] = mapped_column(String(200), index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    message_type: Mapped[str] = mapped_column(String(50), default="text")
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VisualMemory(Base):
    """Screenshot index — stores metadata for all screenshots taken by the browser skill."""

    __tablename__ = "visual_memory"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    file_path: Mapped[str] = mapped_column(String(500), default="")
    url: Mapped[str] = mapped_column(Text, default="", index=True)
    page_title: Mapped[str] = mapped_column(String(500), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list] = mapped_column(ARRAY(String), default=list)
    chat_id: Mapped[str] = mapped_column(String(50), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )


class LearningExample(Base):
    """Learned (input → skill_call) pairs from user feedback."""

    __tablename__ = "learning_examples"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    user_input: Mapped[str] = mapped_column(Text, default="")
    skill_calls: Mapped[str] = mapped_column(Text, default="")   # raw skill call text that worked
    outcome: Mapped[str] = mapped_column(String(20), default="positive")  # positive/negative
    chat_id: Mapped[str] = mapped_column(String(50), default="", index=True)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True,
        default=lambda: datetime.now(timezone.utc),
    )


class KnowledgeNode(Base):
    """Knowledge graph node — an entity the agent knows about."""

    __tablename__ = "knowledge_nodes"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    name: Mapped[str] = mapped_column(String(500), index=True)
    entity_type: Mapped[str] = mapped_column(String(100), default="entity", index=True)
    # person / place / concept / preference / fact / organization / asset
    description: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(default=1.0)
    source_chat_id: Mapped[str] = mapped_column(String(50), default="", index=True)
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class KnowledgeRelation(Base):
    """Knowledge graph edge — a relationship between two entities."""

    __tablename__ = "knowledge_relations"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    from_node_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    to_node_id: Mapped[str] = mapped_column(UUID(as_uuid=False), index=True)
    relation_type: Mapped[str] = mapped_column(String(200), index=True)
    # has_brother / prefers / works_at / owns / dislikes / lives_in / birthday_on / etc.
    value: Mapped[str] = mapped_column(Text, default="")  # optional literal value
    confidence: Mapped[float] = mapped_column(default=1.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )


class DreamLog(Base):
    """Log of dream processing cycles — what the agent processed during idle time."""

    __tablename__ = "dream_log"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    memories_consolidated: Mapped[int] = mapped_column(Integer, default=0)
    kg_nodes_added: Mapped[int] = mapped_column(Integer, default=0)
    reflection: Mapped[str] = mapped_column(Text, default="")
    improvements_proposed: Mapped[int] = mapped_column(Integer, default=0)
    improvements_json: Mapped[list] = mapped_column(JSONB, default=list)
    prefetch_done: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )


class ProceduralMemory(Base):
    """Reusable task procedures abstracted from successful multi-step executions."""

    __tablename__ = "procedural_memory"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    trigger_keywords: Mapped[list] = mapped_column(ARRAY(String), default=list)
    steps: Mapped[list] = mapped_column(JSONB, default=list)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    source_chat_id: Mapped[str] = mapped_column(String(50), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BehavioralRule(Base):
    """Behavioral rule learned from a user correction — auto-injected into every system prompt."""

    __tablename__ = "behavioral_rules"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    rule_type: Mapped[str] = mapped_column(String(50), default="refusal", index=True)
    # refusal / hallucination / wrong_skill / missing_context
    description: Mapped[str] = mapped_column(Text, default="")
    # Clear rule sentence: "When asked to X, always call skill Y"
    skill_poison: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Exact phrase from bad response to filter from episodic memory
    fewshot_user: Mapped[str | None] = mapped_column(Text, nullable=True)
    fewshot_assistant: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Few-shot pair showing correct behavior
    source_exchange: Mapped[dict] = mapped_column(JSONB, default=dict)
    # {user_request, agent_response, user_correction} for audit
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    active: Mapped[bool] = mapped_column(default=True, index=True)
    times_applied: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )


class WorldTimeline(Base):
    """Temporal world model — timestamped observations about entities and events."""

    __tablename__ = "world_timeline"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    entity: Mapped[str] = mapped_column(String(200), index=True)
    observation_type: Mapped[str] = mapped_column(String(100), default="mention", index=True)
    # price / event / state / mention / metric
    value: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(100), default="")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    chat_id: Mapped[str] = mapped_column(String(50), default="", index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ── Next-Gen Cognitive Systems ─────────────────────────────────────────────


class MemoryEmbedding(Base):
    """Vector embeddings for semantic memory search.

    Stores float32 embeddings as JSONB to avoid pgvector dependency.
    Cosine similarity is computed in Python at query time.
    Created automatically by VECTOR_MEMORY_ENABLED=true at startup.
    """

    __tablename__ = "memory_embeddings"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    # Reference to the source item (memory entry id, episodic chunk id, etc.)
    source_id: Mapped[str] = mapped_column(String(200), index=True)
    source_type: Mapped[str] = mapped_column(String(50), default="episodic", index=True)
    # episodic / semantic / procedural / custom
    content_preview: Mapped[str] = mapped_column(Text, default="")  # first 300 chars
    embedding_json: Mapped[list] = mapped_column(JSONB, default=list)  # list[float]
    embed_model: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )


class SkillPattern(Base):
    """Recurring skill usage sequences detected in the audit log.

    Source data for the Skill Evolution Engine (SKILL_EVOLUTION_ENABLED).
    """

    __tablename__ = "skill_patterns"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    # Canonical pattern key, e.g. "web_search→fetch_url→gmail"
    pattern_key: Mapped[str] = mapped_column(String(500), unique=True, index=True)
    skill_sequence: Mapped[list] = mapped_column(JSONB, default=list)  # ordered list of skill names
    occurrence_count: Mapped[int] = mapped_column(Integer, default=0, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    synthesized: Mapped[bool] = mapped_column(default=False)  # True once a composite skill was created
    composite_skill_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class EntityState(Base):
    """Structured world-state snapshot for a tracked entity.

    Updated by the WorldModel layer from world_timeline + knowledge_graph data.
    """

    __tablename__ = "entity_states"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    entity: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    entity_type: Mapped[str] = mapped_column(String(100), default="generic", index=True)
    # crypto / stock / person / place / system / metric / generic
    current_value: Mapped[str] = mapped_column(Text, default="")
    previous_value: Mapped[str] = mapped_column(Text, default="")
    change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    trend: Mapped[str] = mapped_column(String(20), default="stable")
    # up / down / stable / volatile / unknown
    state_metadata: Mapped[dict] = mapped_column(JSONB, default=dict)
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class StatePrediction(Base):
    """LLM-generated forecast for a tracked entity's future state.

    Created by the WorldModel layer's forecast capability.
    """

    __tablename__ = "state_predictions"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    entity: Mapped[str] = mapped_column(String(200), index=True)
    prediction_text: Mapped[str] = mapped_column(Text, default="")
    horizon: Mapped[str] = mapped_column(String(50), default="24h")
    # 1h / 24h / 7d / 30d
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    model_used: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GoalMemory(Base):
    """Goal-scoped observations — memory entries tied to a specific active goal.

    Retrieved only when the matching goal_id is active, preventing unrelated
    memory pollution between concurrent goals.

    Auto-created by SQLAlchemy create_all() on startup — no migration needed.
    """

    __tablename__ = "goal_memory"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    goal_id: Mapped[str] = mapped_column(String(200), index=True)
    observation: Mapped[str] = mapped_column(Text, default="")
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )


class Capability(Base):
    """Learned multi-step capability derived from recurring execution traces."""

    __tablename__ = "capabilities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    trigger_keywords: Mapped[list] = mapped_column(JSON, default=list)
    steps: Mapped[list] = mapped_column(JSON, default=list)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    source_trace_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


class AgentIdentity(Base):
    """Persistent agent identity — birth date and experience points.

    Exactly ONE row (id=1). Created on first startup via get_or_create().
    XP increments on every real user interaction.
    """

    __tablename__ = "agent_identity"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    born_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    total_xp: Mapped[int] = mapped_column(BigInteger, default=0)


class ExecutionKnowledge(Base):
    """Durable storage for execution-level learned knowledge.

    Persists strategy efficiency scores, proven selectors, and global
    cross-domain statistics accumulated by the Execution Reflection Engine.

    Structured as a typed key-value store with JSONB payloads so the schema
    never needs migrating as new knowledge types are added.

    Auto-created by SQLAlchemy create_all() on startup.
    """

    __tablename__ = "execution_knowledge"
    __table_args__ = (
        UniqueConstraint("key_type", "domain", "name", name="uq_execution_knowledge"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # 'strategy_score' | 'selector' | 'global_stat'
    domain: Mapped[str] = mapped_column(String(128), nullable=False, default="", index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # strategy name, element_type, or global stat key
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        index=True,
    )


class ExecutionReflection(Base):
    """Heuristic reflection on a single completed conversation turn.

    Generated after every message by analyzing duration, skills used, and
    outcome.  Pattern detection marks recurring failure/success patterns so
    the agent can learn from its own execution history.

    Auto-created by SQLAlchemy create_all() on startup.
    Retention: capped at 1000 rows via the reflection_pruner scheduler job.
    """

    __tablename__ = "execution_reflections"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    chat_id: Mapped[str] = mapped_column(String(50), default="", index=True)
    intent: Mapped[str] = mapped_column(Text, default="")
    skills_used: Mapped[list] = mapped_column(JSON, default=list)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    efficiency_score: Mapped[float] = mapped_column(Float, default=1.0)
    issues: Mapped[list] = mapped_column(JSON, default=list)
    insight: Mapped[str] = mapped_column(Text, default="")
    suggestion: Mapped[str] = mapped_column(Text, default="")
    pattern_key: Mapped[str] = mapped_column(String(100), default="", index=True)
    recurring_pattern: Mapped[bool] = mapped_column(Boolean, default=False)
    reflection_type: Mapped[str] = mapped_column(String(20), default="optimization")


class Opportunity(Base):
    """Detected proactive opportunity — stored for dashboard visibility and dedup.

    Sources: reflection patterns, KG tech-stack signals, episodic behavior clusters.
    Created by OpportunityEngine on detection; status updated through the lifecycle.
    Auto-created by SQLAlchemy create_all() on startup.
    """

    __tablename__ = "opportunities"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    opp_type: Mapped[str] = mapped_column(String(50), default="suggestion", index=True)
    # optimization / automation / correction / suggestion
    description: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(30), default="pattern", index=True)
    # reflection / KG / pattern
    related_entities: Mapped[list] = mapped_column(JSON, default=list)
    action_policy: Mapped[str] = mapped_column(String(20), default="suggest_only")
    # suggest_only / draft_goal / auto_execute
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    # new / seen / accepted / rejected
    fingerprint: Mapped[str] = mapped_column(
        String(64), default="", index=True, unique=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    suggested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class UserPreference(Base):
    """Per-user persistent preferences, keyed by Telegram chat_id.

    Currently stores language preference so it survives Redis flushes and
    container restarts. One row per chat_id — upserted on every language update.
    Auto-created by SQLAlchemy create_all() on startup.
    """

    __tablename__ = "user_preferences"

    chat_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    language: Mapped[str] = mapped_column(String(10), default="en", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


# Phase 5 — Memory truth model: stable per-user attributes with versioning.
# Episodic memory_entries stores raw user utterances; this table promotes
# specific declared facts (name, email, pets, colors, etc.) to a structured
# row that the agent can read deterministically. Contradictions DO NOT
# overwrite — the system asks the user to disambiguate.
class UserAttribute(Base):
    """A single declared fact about a user.

    Composite PK (chat_id, key) ensures one current value per attribute.
    Prior values are preserved in attribute_history for audit.
    """
    __tablename__ = "user_attributes"

    chat_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    source: Mapped[str] = mapped_column(String(50), default="user_declaration", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class UserAttributeHistory(Base):
    """Audit trail for user_attributes — every previous value preserved.

    Append-only. Used to (a) detect repeated gaslighting, (b) show user
    "you previously told me X" when contradicting.
    """
    __tablename__ = "user_attribute_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(80), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(50), default="user_declaration", nullable=False)
    superseded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    __table_args__ = (
        Index("ix_uah_chat_key", "chat_id", "key"),
    )
