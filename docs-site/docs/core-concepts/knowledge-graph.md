---
id: knowledge-graph
title: Knowledge Graph
description: Entity-relation memory with rule-based extraction and Redis cache.
---

# Knowledge Graph

The knowledge graph (KG) records entities, their relations, and the operator's preferences. It is built incrementally from every conversation and injected into every system prompt.

## Storage

| Layer | Backing | Purpose |
|-------|---------|---------|
| Nodes | `KnowledgeNode` (Postgres) | id, name, entity_type, description, confidence, source_chat_id, metadata, created_at |
| Relations | `KnowledgeRelation` (Postgres) | from_node_id → to_node_id, relation_type, value, confidence |
| Hot cache | `kg:node:{id}` (Redis HASH) | Fast reads of node attributes |
| Lookup | `kg:index` (Redis HASH) | `name_lower → node_id` for entity-name resolution |

## Entity types

```
person, place, concept, preference, fact,
organization, asset, skill, event, time
```

The classifier in `extract_from_conversation()` routes new entities to the appropriate type. Confidence is initialized at 0.6 and updated on each subsequent mention.

## Extraction

After every chat turn, `extract_from_conversation()` runs. It uses three layers:

1. **Rule-based regex extraction**:
   - `_PREFERENCE_PATTERNS` — extracts user preferences ("I prefer X", "I hate Y")
   - `_PERSON_PATTERNS` — extracts named people
   - `_SOURCE_PATTERNS` — extracts cited sources
2. **LLM extraction** (when rule-based finds nothing) — short prompt asking for entities + relations.
3. **Deduplication** — `kg:index` lookup; same name → update confidence, not create new node.

Both layers are fire-and-forget — extraction failures don't block the response.

## Relations

Examples of relation types:

```
mentions, prefers, dislikes, owns,
works_at, located_in, is_a, refers_to,
created_by, derived_from
```

Each relation has a `confidence` (0–1) and an optional `value` (for typed relations like "ETH price = 3500").

## Injection

`format_for_context()` produces a compact text block injected into the system prompt per chat:

```
[KNOWLEDGE GRAPH — RECENT FACTS]
- alice (person, conf=0.9): user's colleague, works at corp
- ETH (asset, conf=0.95): user holds 4.5 ETH
- prefers brevity (preference, conf=0.85)
```

Block size is bounded; only the highest-confidence and most-recent facts are included.

## Browse and edit

Dashboard `/knowledge-graph`:

- Force-directed canvas with physics simulation (repulsion, link attraction, center pull, drag, click-to-select)
- 8-color type palette
- Node detail overlay
- Per-node delete button
- Search by name

## Lifecycle

| Action | Effect |
|--------|--------|
| Create | New node with confidence=0.6 |
| Mention again | Confidence += `KG_CONFIDENCE_INCREMENT` (default 0.05) |
| Reject (operator delete) | Hard-delete from Postgres + Redis cache |
| Decay | Currently no automatic decay; a stale node lives until manually deleted or Panic Reset |

## Panic reset

`/reset` truncates `knowledge_nodes` and `knowledge_relations`, plus wipes all `kg:*` Redis keys. Useful when contamination is suspected.

## See also

- [Memory](/core-concepts/memory) — overall memory system
- [World Model](/cognitive-systems/world-model) — temporal observations of entities
- [Temporal Reasoning](/cognitive-systems/temporal-reasoning) — entity-history queries
