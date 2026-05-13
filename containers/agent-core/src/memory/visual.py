"""Visual memory — indexes screenshots taken by the browser skill.

Auto-stores metadata (URL, title, path, description) when a screenshot is taken.
Enables the agent to recall and search past visual observations.
"""

import structlog
from uuid import uuid4
from datetime import datetime, timezone

from ..db.models import VisualMemory
from ..db.session import async_session

logger = structlog.get_logger()


async def store_screenshot(
    file_path: str,
    url: str = "",
    page_title: str = "",
    description: str = "",
    tags: list[str] | None = None,
    chat_id: str = "",
) -> str:
    """Store screenshot metadata in the visual memory index. Returns the entry ID."""
    entry_id = str(uuid4())
    try:
        async with async_session() as session:
            entry = VisualMemory(
                id=entry_id,
                file_path=file_path,
                url=url,
                page_title=page_title,
                description=description,
                tags=tags or [],
                chat_id=chat_id,
            )
            session.add(entry)
            await session.commit()
        logger.info("visual_memory.stored", id=entry_id, url=url, path=file_path)
    except Exception:
        logger.exception("visual_memory.store_error")
    return entry_id


async def search_screenshots(
    keyword: str = "",
    url_contains: str = "",
    chat_id: str = "",
    limit: int = 10,
) -> list[dict]:
    """Search visual memory entries by keyword, URL, or chat."""
    from sqlalchemy import select, or_
    results = []
    try:
        async with async_session() as session:
            q = select(VisualMemory).order_by(VisualMemory.created_at.desc()).limit(limit)
            if chat_id:
                q = q.where(VisualMemory.chat_id == chat_id)
            if url_contains:
                q = q.where(VisualMemory.url.ilike(f"%{url_contains}%"))
            if keyword:
                q = q.where(
                    or_(
                        VisualMemory.description.ilike(f"%{keyword}%"),
                        VisualMemory.page_title.ilike(f"%{keyword}%"),
                    )
                )
            rows = await session.execute(q)
            for row in rows.scalars():
                results.append({
                    "id": row.id,
                    "file_path": row.file_path,
                    "url": row.url,
                    "page_title": row.page_title,
                    "description": row.description,
                    "tags": row.tags,
                    "created_at": row.created_at.isoformat() if row.created_at else "",
                })
    except Exception:
        logger.exception("visual_memory.search_error")
    return results


async def get_recent_screenshots(limit: int = 5, chat_id: str = "") -> list[dict]:
    """Get the most recent screenshots."""
    return await search_screenshots(chat_id=chat_id, limit=limit)


def format_visual_context(entries: list[dict]) -> str:
    """Format visual memory entries for injection into LLM context."""
    if not entries:
        return ""
    lines = ["[VISUAL MEMORY — recent screenshots:]"]
    for e in entries:
        ts = e.get("created_at", "")[:16]
        url = e.get("url", "")
        title = e.get("page_title", "")
        path = e.get("file_path", "")
        desc = e.get("description", "")
        lines.append(f"  • {ts} | {title} | {url}\n    Path: {path}" + (f"\n    {desc}" if desc else ""))
    return "\n".join(lines)
