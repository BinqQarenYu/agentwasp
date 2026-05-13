"""Agent-to-agent message bus — PostgreSQL-backed.

Messages are persistent and queryable. No Redis needed for durability —
PostgreSQL provides the ACID guarantees needed for inter-agent coordination.

Message types:
  text          — plain text message
  task_result   — result of a completed goal/task
  request       — request for another agent to perform an action
  notification  — informational update
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import structlog

from ..db.models import AgentMessage as AgentMessageModel
from ..db.session import async_session

logger = structlog.get_logger()


async def send_message(
    from_agent_id: str,
    to_agent_id: str,
    content: str,
    message_type: str = "text",
    metadata: dict | None = None,
) -> AgentMessageModel:
    """Create and persist an inter-agent message.

    Returns the saved AgentMessage ORM instance.
    """
    msg = AgentMessageModel(
        id=str(uuid4()),
        from_agent_id=from_agent_id,
        to_agent_id=to_agent_id,
        content=content,
        message_type=message_type,
        metadata_json=metadata or {},
        created_at=datetime.now(timezone.utc),
    )
    async with async_session() as session:
        session.add(msg)
        await session.commit()
        await session.refresh(msg)
    logger.info(
        "agent_bus.message_sent",
        from_id=from_agent_id,
        to_id=to_agent_id,
        type=message_type,
    )
    return msg


async def get_messages(
    agent_id: str,
    limit: int = 20,
    unread_only: bool = False,
) -> list[AgentMessageModel]:
    """Retrieve messages sent TO agent_id, newest first."""
    from sqlalchemy import select
    async with async_session() as session:
        stmt = (
            select(AgentMessageModel)
            .where(AgentMessageModel.to_agent_id == agent_id)
            .order_by(AgentMessageModel.created_at.desc())
            .limit(limit)
        )
        if unread_only:
            stmt = stmt.where(AgentMessageModel.read_at.is_(None))
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def mark_read(message_id: str) -> None:
    """Mark a message as read."""
    from sqlalchemy import select
    async with async_session() as session:
        result = await session.execute(
            select(AgentMessageModel).where(AgentMessageModel.id == message_id)
        )
        msg = result.scalar_one_or_none()
        if msg and msg.read_at is None:
            msg.read_at = datetime.now(timezone.utc)
            await session.commit()


async def get_sent_messages(
    agent_id: str,
    limit: int = 20,
) -> list[AgentMessageModel]:
    """Retrieve messages sent BY agent_id, newest first."""
    from sqlalchemy import select
    async with async_session() as session:
        result = await session.execute(
            select(AgentMessageModel)
            .where(AgentMessageModel.from_agent_id == agent_id)
            .order_by(AgentMessageModel.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())
