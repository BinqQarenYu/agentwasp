"""System 1 — Vector Semantic Memory.

Semantic similarity search over memory entries using real vector embeddings.

Architecture:
  - Embeddings generated via a pluggable EmbeddingProvider (Ollama, OpenAI, or hash fallback).
  - Provider selected at runtime from settings.embedding_provider.
  - Vectors stored as JSONB in `memory_embeddings` table (no pgvector needed).
  - Cosine similarity computed in Python at query time.
  - Fully feature-flagged: VECTOR_MEMORY_ENABLED=false → all ops are no-ops.
  - Degraded mode: when provider.is_semantic is False, a warning is logged so
    operators know retrieval quality is reduced.

Integration points:
  - Called from build_context() to inject semantically relevant memories.
  - Called from VectorIndexJob (scheduler) to asynchronously index new content.
  - EmbeddingProvider created via memory.embeddings.create_provider(settings).
"""

from __future__ import annotations

import math
import operator
import time
from typing import TYPE_CHECKING
from uuid import uuid4

import structlog

from .embeddings import EmbeddingProvider
from .embeddings.hash_fallback import HashFallbackEmbeddingProvider

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

_EMBED_DIM = 384  # Normalised output dimensionality


# ---------------------------------------------------------------------------
# Low-level vector utilities (unchanged — used by providers and tests)
# ---------------------------------------------------------------------------


def hash_embedding(text: str, dims: int = _EMBED_DIM) -> list[float]:
    """Deterministic pseudo-embedding from text hash.

    Kept as a standalone utility for code that needs a synchronous fallback.
    Prefer HashFallbackEmbeddingProvider for new code.
    """
    import hashlib, struct
    raw = hashlib.sha512(text.encode()).digest()
    needed = dims * 4
    repeated = (raw * (needed // len(raw) + 1))[:needed]
    floats = [struct.unpack_from("f", repeated, i * 4)[0] for i in range(dims)]
    floats = [0.0 if not math.isfinite(f) else f for f in floats]
    return _normalise(floats)


def _normalise(vec: list[float]) -> list[float]:
    """L2-normalise a vector (unit vector). Returns zero-vector if norm == 0."""
    if not vec:
        return vec
    # Bolt: math.hypot(*vec) is ~2x faster than math.sqrt(sum(x * x for x in vec)) in pure Python
    norm = math.hypot(*vec)
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two normalised unit vectors."""
    if len(a) != len(b) or not a:
        return 0.0
    # Bolt: sum(map(operator.mul, a, b)) is ~1.5x faster than sum(x * y for x, y in zip(a, b))
    return sum(map(operator.mul, a, b))


# ---------------------------------------------------------------------------
# Internal embed helper — provider + hash fallback chain
# ---------------------------------------------------------------------------


async def _embed_with_fallback(
    text: str,
    provider: EmbeddingProvider,
) -> tuple[list[float], str]:
    """Embed text using *provider*, falling back to hash if provider returns None.

    Returns (vector, model_name_used).
    Logs a warning when degraded mode is active.
    """
    vec = await provider.embed(text)
    if vec is not None:
        return vec, provider.model_name

    # Provider returned None — activate hash fallback
    if provider.is_semantic:
        # Semantic provider failed (Ollama offline, API error, etc.)
        logger.warning(
            "vector_memory.provider_failed_degraded",
            provider=provider.model_name,
            fallback="hash-fallback",
        )
    fallback = HashFallbackEmbeddingProvider()
    vec = await fallback.embed(text)
    return vec, fallback.model_name  # type: ignore[return-value]


def _warn_if_degraded(provider: EmbeddingProvider) -> None:
    """Log a warning once when non-semantic provider is in use."""
    if not provider.is_semantic:
        logger.warning(
            "vector_memory.degraded_mode_active",
            provider=provider.model_name,
            note="Semantic retrieval quality is reduced. Pull an embedding model or configure embedding_provider.",
        )


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------


async def store_embedding(
    session: "AsyncSession",
    source_id: str,
    source_type: str,
    content: str,
    provider: EmbeddingProvider,
) -> bool:
    """Generate and persist an embedding for a piece of content.

    Returns True on success, False on failure.
    provider — an EmbeddingProvider instance (from memory.embeddings.create_provider).
    """
    from ..db.models import MemoryEmbedding
    from sqlalchemy import select

    _warn_if_degraded(provider)

    # Skip if already indexed
    existing = (
        await session.execute(
            select(MemoryEmbedding).where(
                MemoryEmbedding.source_id == source_id,
                MemoryEmbedding.source_type == source_type,
            )
        )
    ).scalar_one_or_none()
    if existing:
        return True  # Already indexed

    vec, used_model = await _embed_with_fallback(content[:500], provider)

    entry = MemoryEmbedding(
        id=str(uuid4()),
        source_id=source_id,
        source_type=source_type,
        content_preview=content[:300],
        embedding_json=vec,
        embed_model=used_model,
    )
    session.add(entry)
    try:
        await session.commit()
        return True
    except Exception as exc:
        await session.rollback()
        logger.warning("vector_memory.store_failed", error=str(exc)[:120])
        return False


async def semantic_search(
    session: "AsyncSession",
    query: str,
    provider: EmbeddingProvider,
    source_type: str | None = None,
    top_k: int = 8,
) -> list[dict]:
    """Find the most semantically similar stored memories to a query.

    Returns a list of dicts sorted by similarity (descending):
      [{"source_id": ..., "source_type": ..., "preview": ..., "score": float,
        "embed_model": str}, ...]

    provider — an EmbeddingProvider instance (from memory.embeddings.create_provider).
    """
    from ..db.models import MemoryEmbedding
    from sqlalchemy import select

    _warn_if_degraded(provider)

    t0 = time.monotonic()

    query_vec, _ = await _embed_with_fallback(query[:500], provider)

    # Load candidate embeddings (limit 2000 to bound memory usage)
    q = select(MemoryEmbedding)
    if source_type:
        q = q.where(MemoryEmbedding.source_type == source_type)
    q = q.order_by(MemoryEmbedding.created_at.desc()).limit(2000)

    rows = (await session.execute(q)).scalars().all()
    if not rows:
        return []

    # Score by cosine similarity
    scored: list[tuple[float, MemoryEmbedding]] = []
    for row in rows:
        vec = row.embedding_json
        if isinstance(vec, list) and vec:
            sim = cosine_similarity(query_vec, vec)
            scored.append((sim, row))

    scored.sort(key=lambda x: x[0], reverse=True)
    results = [
        {
            "source_id": row.source_id,
            "source_type": row.source_type,
            "preview": row.content_preview,
            "score": round(score, 4),
            "embed_model": row.embed_model,
        }
        for score, row in scored[:top_k]
        if score > 0.3  # discard low-similarity results
    ]

    logger.debug(
        "vector_memory.search_done",
        query_len=len(query),
        candidates=len(rows),
        results=len(results),
        latency_ms=round((time.monotonic() - t0) * 1000),
        provider=provider.model_name,
    )
    return results


# ---------------------------------------------------------------------------
# Context formatting
# ---------------------------------------------------------------------------


def format_for_context(results: list[dict]) -> str:
    """Format semantic search results as a context block for injection."""
    if not results:
        return ""
    lines = ["[MEMORIA SEMÁNTICA RELEVANTE]"]
    for i, r in enumerate(results, 1):
        score_pct = round(r["score"] * 100)
        lines.append(f"  {i}. [{score_pct}% similar] {r['preview']}")
    return "\n".join(lines)
