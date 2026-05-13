---
id: memory
title: Memory
description: 10 persistent memory layers, ranking, injection budget, reset levels.
---

# Memory

WASP keeps state in three locations: PostgreSQL (durable), Redis (fast/cached), and the filesystem (`/data/memory/` for backups, `/data/screenshots/` for visual artifacts). Ten primary memory layers persist across restarts.

## Layer overview

| Layer | Backing | Purpose | Improves with use? |
|-------|---------|---------|--------------------|
| Episodic | `MemoryEntry` (Postgres) | Conversation history, file references | Yes — every turn appends |
| Semantic / vector | `MemoryEmbedding` (Postgres, JSONB) | Embedding-based similarity retrieval | Yes — embeddings on promotion |
| Knowledge graph | `KnowledgeNode`, `KnowledgeRelation` (Postgres) + `kg:node:*` (Redis) | Entities, relations, preferences | Yes — extracted from conversations |
| Procedural | `ProceduralMemory` (Postgres) | Multi-step procedures abstracted from successful runs | Yes — learned from execution |
| Behavioral | `BehavioralRule` (Postgres) | Rules learned from user corrections | Yes — corrections trigger learning |
| Learning examples | `LearningExample` (Postgres) | Positive/negative few-shots | Yes — feedback adds examples |
| Visual | `VisualMemory` (Postgres) | Indexed screenshots with metadata | Yes — every browser capture |
| Goal-scoped | `GoalMemory` (Postgres) | Per-goal observations during execution | Yes — bounded to goal lifetime |
| Temporal world model | `WorldTimeline`, `EntityState`, `StatePrediction` (Postgres) | Time-series of entities (prices, system metrics) | Yes — extracted from skill outputs |
| Ranking | (computed) | Composite retrieval scoring | No — algorithm |

Each layer has a dedicated topic page in [Cognitive Systems](/cognitive-systems/world-model). This page focuses on the foundational layers (episodic, semantic, ranking) and the global picture.

## Episodic memory

`memory/index.py`, `memory/store.py`.

Every chat turn writes a `MemoryEntry` row:

```
id (UUID), memory_type (enum), project_id, chat_id,
file_path, tags (JSONB), content_summary, content_hash,
importance, access_count, created_at, version
```

`MemoryManager.add_episodic()` inserts; `query_episodic()` retrieves with ranking.

`PromotionEngine` scans episodic entries and promotes those that recur or have high importance into semantic memory. Runs on a schedule (`promotion` job, 12 h) plus inside the dream cycle.

`ForgettingCurve` decays stale entries; `MemoryCleanupJob` (daily) drops entries below the importance floor.

## Semantic / vector memory

`memory/vector_memory.py`, `memory/embeddings/*.py`.

Embeddings are stored as `MemoryEmbedding` rows with the embedding as JSONB float32. Three embedding backends:

| Backend | When | File |
|---------|------|------|
| OpenAI | If `OPENAI_API_KEY` present and `EMBEDDING_PROVIDER=openai` | `embeddings/openai.py` |
| Ollama | If `--profile local-llm` is up | `embeddings/ollama.py` |
| Hash fallback | Always available; deterministic SHA-512 vector | `embeddings/hash_fallback.py` |

Cosine similarity search (`vector_search(query, top_k)`) is the primary retrieval. The Vector Index Job (`vector_index`, default 600 s) backfills missing embeddings.

## Ranking

`memory/ranking.py`.

Composite relevance scoring:

```
score = 0.5 × similarity + 0.3 × recency + 0.2 × importance
```

`_recency_score()` is exponential decay (half_life_hours = 24).

Applied to retrieved memories before injection.

## Memory injection budget

The Context Builder injects memory into every prompt with caps to control cost:

| Layer | Cap |
|-------|-----|
| Episodic | adaptive (last N exchanges, sized to remaining budget) |
| Semantic / vector | 5 entries |
| Procedural | 3 procedures |
| Goal-scoped | 5 observations |
| Knowledge graph | (compact format, varies) |
| Behavioral rules | all active rules |
| Self-model | strengths + known failures + active preferences |
| Epistemic state | high/medium/low summary |
| Temporal | recent observations relevant to current text |

Total memory block typically 2–8 KB. Progressive truncation when approaching model context window: full → 4 exchanges → 2 → 1, system prompt always preserved. Logged as `model_manager.overflow_recovered`.

## Auxiliary state

Two agent-state Redis keys complement the SQL layers:

| Key | Module | Purpose |
|-----|--------|---------|
| `agent:self_model` (+ `:version`) | `agent/self_model.py` | Strengths, known failures, user preferences, weekly stats; file backup at `/data/memory/self_model.json` |
| `agent:epistemic` | `agent/epistemic.py` | Per-domain confidence; symmetric ±0.015 calibration on skill success/failure |
| `agent:cpi` (+ `:cpi_high`) | `agent/cpi.py` | Composite cognitive pressure 0–100 |

## Resetting memory

Three escalation levels:

| Level | Action |
|-------|--------|
| Per-chat | `/wipe_all` Telegram command — drops episodic + flow lock for the current chat |
| Per-layer | SQL `DELETE` from a specific table; or dashboard `/memory`, `/behavioral-rules`, `/knowledge-graph` per-row delete |
| Full reset | Dashboard `/reset` (Panic Reset) — wipes 17 tables + 12+ Redis key patterns + agent identity, runs `VACUUM FULL` |

Panic Reset never erases: API keys, Docker volumes, custom Python skills, subscriptions, prime.md, `/data/src_patches/` backups.

## See also

- [Knowledge Graph](/core-concepts/knowledge-graph) — entity-relation memory
- [Cognitive Systems → Behavioral Learning](/cognitive-systems/behavioral-learning)
- [Cognitive Systems → World Model](/cognitive-systems/world-model)
- [Cognitive Systems → Temporal Reasoning](/cognitive-systems/temporal-reasoning)
- [Architecture → Context Builder](/architecture/context-builder) — memory injection detail
