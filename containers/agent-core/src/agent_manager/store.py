"""Agent persistence — Redis HASH as primary store + PostgreSQL for queryability.

Storage layout:
  REDIS_KEY (HASH) = "agents"
    field: agent_id (str)
    value: JSON-serialized Agent

All reads/writes use the Agent Pydantic model for schema validation.
PostgreSQL AgentRecord is updated on every save for queryability.
"""

from __future__ import annotations

import structlog

from ..db.models import AgentRecord
from ..db.session import async_session
from .types import Agent, AgentStatus

logger = structlog.get_logger()

REDIS_KEY = "agents"


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------


async def save_agent(r, agent: Agent) -> None:
    """Persist agent state to Redis HASH and PostgreSQL."""
    agent.touch()
    await r.hset(REDIS_KEY, agent.id, agent.model_dump_json())
    # Mirror to PostgreSQL for queryability (upsert by id to avoid unique-name race conditions)
    try:
        from datetime import datetime, timezone
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        _now = datetime.now(timezone.utc)
        async with async_session() as session:
            stmt = pg_insert(AgentRecord).values(
                id=agent.id,
                name=agent.name,
                status=agent.status.value,
                model_provider=agent.model_provider,
                model_name=agent.model_name,
                memory_namespace=agent.effective_namespace,
                autonomy_mode=agent.autonomy_mode.value,
                updated_at=_now,
            ).on_conflict_do_update(
                index_elements=["id"],
                set_={
                    "name": agent.name,
                    "status": agent.status.value,
                    "model_provider": agent.model_provider,
                    "model_name": agent.model_name,
                    "memory_namespace": agent.effective_namespace,
                    "autonomy_mode": agent.autonomy_mode.value,
                    "updated_at": _now,
                },
            )
            await session.execute(stmt)
            await session.commit()
    except Exception:
        logger.exception("agent_store.pg_write_error", agent_id=agent.id)


async def load_agent(r, agent_id: str) -> Agent | None:
    """Load a single agent by ID. Returns None if not found."""
    raw = await r.hget(REDIS_KEY, agent_id)
    if not raw:
        return None
    try:
        return Agent.model_validate_json(raw)
    except Exception:
        logger.exception("agent_store.load_error", agent_id=agent_id)
        return None


async def list_agents(r) -> list[Agent]:
    """Return all agents sorted by created_at descending."""
    raw_map = await r.hgetall(REDIS_KEY)
    agents: list[Agent] = []
    for v in raw_map.values():
        try:
            agents.append(Agent.model_validate_json(v))
        except Exception:
            pass
    agents.sort(key=lambda a: a.created_at, reverse=True)
    return agents


async def delete_agent(r, agent_id: str) -> bool:
    """Hard-delete an agent from Redis AND PostgreSQL. Returns True if it existed."""
    result = await r.hdel(REDIS_KEY, agent_id)
    # Also remove from Postgres to prevent unique-name constraint violations on re-creation
    try:
        async with async_session() as session:
            from sqlalchemy import delete as sql_delete
            await session.execute(
                sql_delete(AgentRecord).where(AgentRecord.id == agent_id)
            )
            await session.commit()
    except Exception:
        logger.exception("agent_store.pg_delete_error", agent_id=agent_id)
    return result > 0


async def list_active_agents(r) -> list[Agent]:
    """Return agents in RUNNING state only."""
    return [a for a in await list_agents(r) if a.status == AgentStatus.RUNNING]
