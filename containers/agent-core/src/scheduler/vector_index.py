"""VectorIndexJob — scheduler job for System 1 (Vector Semantic Memory).

Periodically indexes recent episodic memories with vector embeddings.
Runs every 1800s (30 min) when VECTOR_MEMORY_ENABLED=true.

Safe to disable: when vector_memory_enabled=false this job is never registered.
"""

from __future__ import annotations

import time

import structlog

from ..memory.embeddings import EmbeddingProvider

logger = structlog.get_logger()


class VectorIndexJob:
    """Indexes recent un-embedded memory entries using a pluggable EmbeddingProvider."""

    def __init__(
        self,
        provider: EmbeddingProvider,
        batch_size: int = 20,
    ):
        self._provider = provider
        self._batch_size = batch_size

    async def __call__(self) -> str:
        from ..db.session import async_session
        from ..db.models import MemoryEmbedding
        from ..memory.vector_memory import store_embedding
        from sqlalchemy import select

        t0 = time.monotonic()
        indexed = 0
        errors = 0

        try:
            async with async_session() as session:
                already_indexed: set[str] = set(
                    (
                        await session.execute(
                            select(MemoryEmbedding.source_id).where(
                                MemoryEmbedding.source_type == "episodic"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )

                from ..db.models import MemoryEntry
                rows = (
                    await session.execute(
                        select(MemoryEntry)
                        .where(
                            MemoryEntry.memory_type == "episodic",
                            MemoryEntry.content_summary != "",
                        )
                        .order_by(MemoryEntry.created_at.desc())
                        .limit(self._batch_size * 3)
                    )
                ).scalars().all()

                to_index = [r for r in rows if r.id not in already_indexed][: self._batch_size]

                for entry in to_index:
                    ok = await store_embedding(
                        session=session,
                        source_id=entry.id,
                        source_type="episodic",
                        content=entry.content_summary[:500],
                        provider=self._provider,
                    )
                    if ok:
                        indexed += 1
                    else:
                        errors += 1

        except Exception as exc:
            logger.warning("vector_index_job.error", error=str(exc)[:120])
            errors += 1

        latency_ms = round((time.monotonic() - t0) * 1000)
        result = f"Vector index: {indexed} indexed, {errors} errors ({latency_ms}ms)"
        logger.info("vector_index_job.done", indexed=indexed, errors=errors, latency_ms=latency_ms)
        return result
