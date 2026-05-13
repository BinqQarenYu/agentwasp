from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import delete, select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import MemoryEntry as MemoryEntryRow
from .types import MemoryContent, MemoryQuery

import structlog

logger = structlog.get_logger()


class MemoryIndex:
    """PostgreSQL index for fast memory queries."""

    async def upsert(self, session: AsyncSession, entry: MemoryContent, file_path: str, content_hash: str):
        """Insert or update a memory entry in the index."""
        existing = await session.execute(
            select(MemoryEntryRow).where(MemoryEntryRow.id == entry.id)
        )
        row = existing.scalar_one_or_none()

        if row:
            row.memory_type = entry.memory_type.value
            row.project_id = entry.project_id
            row.file_path = file_path
            row.tags = entry.tags
            row.content_summary = entry.summary
            row.content_hash = content_hash
            row.updated_at = datetime.now(timezone.utc)
            row.version = entry.version
        else:
            row = MemoryEntryRow(
                id=entry.id,
                memory_type=entry.memory_type.value,
                project_id=entry.project_id,
                file_path=file_path,
                tags=entry.tags,
                content_summary=entry.summary,
                content_hash=content_hash,
                created_at=datetime.fromisoformat(entry.created_at),
                updated_at=datetime.now(timezone.utc),
                version=entry.version,
            )
            session.add(row)

        await session.commit()
        logger.debug("memory_index.upsert", id=entry.id, memory_type=entry.memory_type)

    async def remove(self, session: AsyncSession, memory_id: str):
        """Remove a memory entry from the index."""
        await session.execute(
            delete(MemoryEntryRow).where(MemoryEntryRow.id == memory_id)
        )
        await session.commit()

    async def search(self, session: AsyncSession, query: MemoryQuery) -> list[MemoryEntryRow]:
        """Search memory entries in the index."""
        stmt = select(MemoryEntryRow)

        if query.memory_type:
            stmt = stmt.where(MemoryEntryRow.memory_type == query.memory_type.value)

        if query.project_id:
            stmt = stmt.where(MemoryEntryRow.project_id == query.project_id)

        if query.tags:
            stmt = stmt.where(MemoryEntryRow.tags.overlap(query.tags))

        if query.text_search:
            pattern = f"%{query.text_search}%"
            stmt = stmt.where(
                or_(
                    MemoryEntryRow.content_summary.ilike(pattern),
                    MemoryEntryRow.tags.any(query.text_search),
                )
            )

        stmt = stmt.order_by(MemoryEntryRow.updated_at.desc())
        stmt = stmt.offset(query.offset).limit(query.limit)

        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def count(self, session: AsyncSession, memory_type: str | None = None) -> int:
        """Count indexed memory entries."""
        stmt = select(func.count(MemoryEntryRow.id))
        if memory_type:
            stmt = stmt.where(MemoryEntryRow.memory_type == memory_type)
        result = await session.execute(stmt)
        return result.scalar_one()

    async def get_by_id(self, session: AsyncSession, memory_id: str) -> MemoryEntryRow | None:
        result = await session.execute(
            select(MemoryEntryRow).where(MemoryEntryRow.id == memory_id)
        )
        return result.scalar_one_or_none()
