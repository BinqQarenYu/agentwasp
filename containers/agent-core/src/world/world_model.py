"""System 4 — World Model Layer.

Provides a structured, continuously-updated representation of the external world.

Architecture:
  ┌──────────────────────────────────────────────────────────────┐
  │  world_timeline (existing) ──► WorldModel ──► entity_states │
  │  knowledge_graph (existing) ──► WorldModel ──► state_predictions │
  └──────────────────────────────────────────────────────────────┘

Capabilities:
  1. Entity state tracking  — current vs. previous value + trend direction
  2. Causal relation detection — "interest_rates ↑ → crypto_volatility ↑"
  3. Trend analysis          — min/max/avg/direction over configurable window
  4. LLM-powered forecasting — short-horizon predictions stored + cached
  5. Context block           — format_for_context() → [WORLD MODEL] injection

Integration:
  - WorldModelUpdateJob (scheduler) calls update_all_entities() every 15 min.
  - build_context() calls format_for_context() (feature-flagged).
  - AutonomousGoalGeneratorJob can read entity_states for proactive goals.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import structlog

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from ..models.manager import ModelManager

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Trend analysis
# ---------------------------------------------------------------------------


def _parse_numeric(value: str) -> float | None:
    """Extract a numeric value from a string like '$45,231.50' or '3.14%'."""
    import re
    m = re.search(r"[\d,]+\.?\d*", value.replace(",", ""))
    if m:
        try:
            return float(m.group().replace(",", ""))
        except ValueError:
            pass
    return None


def _trend_direction(values: list[float]) -> str:
    """Classify trend from a sequence of numeric observations."""
    if len(values) < 2:
        return "unknown"
    first, last = values[0], values[-1]
    if first == 0:
        return "stable"
    change = (last - first) / abs(first)
    if change > 0.05:
        return "up"
    if change < -0.05:
        return "down"
    # Check volatility (std-dev)
    avg = sum(values) / len(values)
    variance = sum((v - avg) ** 2 for v in values) / len(values)
    import math
    std = math.sqrt(variance)
    if avg and std / avg > 0.05:
        return "volatile"
    return "stable"


# ---------------------------------------------------------------------------
# Core WorldModel class
# ---------------------------------------------------------------------------


class WorldModel:
    """Structured world-state manager."""

    def __init__(self, database_url: str = "", ollama_url: str = ""):
        self._database_url = database_url
        self._ollama_url = ollama_url

    # ── Entity state management ────────────────────────────────────────

    async def update_entity_state(
        self,
        session: AsyncSession,
        entity: str,
        entity_type: str = "generic",
    ) -> dict | None:
        """Pull recent world_timeline observations for an entity and update entity_states."""
        from ..db.models import EntityState, WorldTimeline
        from sqlalchemy import select

        cutoff = datetime.now(timezone.utc) - timedelta(hours=72)
        obs_rows = (
            await session.execute(
                select(WorldTimeline)
                .where(
                    WorldTimeline.entity == entity,
                    WorldTimeline.observed_at >= cutoff,
                )
                .order_by(WorldTimeline.observed_at.asc())
                .limit(100)
            )
        ).scalars().all()

        if not obs_rows:
            return None

        # Build time-series of numeric values
        values: list[float] = []
        latest_raw = obs_rows[-1].value
        latest_type = obs_rows[-1].observation_type
        for row in obs_rows:
            v = _parse_numeric(row.value)
            if v is not None:
                values.append(v)

        trend = _trend_direction(values) if len(values) >= 2 else "unknown"
        change_pct: float | None = None
        if len(values) >= 2 and values[0] != 0:
            change_pct = round((values[-1] - values[0]) / abs(values[0]) * 100, 2)

        # Upsert EntityState
        existing: EntityState | None = (
            await session.execute(
                select(EntityState).where(EntityState.entity == entity)
            )
        ).scalar_one_or_none()

        if existing:
            previous_value = existing.current_value
            existing.previous_value = previous_value
            existing.current_value = latest_raw
            existing.change_pct = change_pct
            existing.trend = trend
            existing.entity_type = entity_type or existing.entity_type
            existing.last_updated = datetime.now(timezone.utc)
            existing.state_metadata = {
                "observation_count": len(obs_rows),
                "latest_type": latest_type,
                "numeric_samples": len(values),
            }
        else:
            existing = EntityState(
                id=str(uuid4()),
                entity=entity,
                entity_type=entity_type,
                current_value=latest_raw,
                previous_value="",
                change_pct=change_pct,
                trend=trend,
                state_metadata={
                    "observation_count": len(obs_rows),
                    "latest_type": latest_type,
                    "numeric_samples": len(values),
                },
            )
            session.add(existing)

        try:
            await session.commit()
        except Exception as exc:
            await session.rollback()
            logger.warning("world_model.upsert_failed", entity=entity, error=str(exc)[:120])
            return None

        return {
            "entity": entity,
            "current": latest_raw,
            "trend": trend,
            "change_pct": change_pct,
        }

    async def update_all_entities(self, session: AsyncSession) -> int:
        """Update state for all entities seen in world_timeline in the last 72h."""
        from ..db.models import WorldTimeline
        from sqlalchemy import select, distinct

        cutoff = datetime.now(timezone.utc) - timedelta(hours=72)
        entity_rows = (
            await session.execute(
                select(distinct(WorldTimeline.entity)).where(
                    WorldTimeline.observed_at >= cutoff
                )
            )
        ).scalars().all()

        updated = 0
        for entity in entity_rows:
            if not entity:
                continue
            result = await self.update_entity_state(session, entity)
            if result:
                updated += 1

        logger.info("world_model.entities_updated", count=updated)
        return updated

    # ── Forecasting ───────────────────────────────────────────────────

    async def generate_forecast(
        self,
        session: AsyncSession,
        entity: str,
        model_manager: ModelManager,
        horizon: str = "24h",
    ) -> str | None:
        """Generate an LLM forecast for an entity. Returns prediction text."""
        from ..db.models import EntityState, StatePrediction, WorldTimeline
        from ..models.types import Message, ModelRequest
        from sqlalchemy import select

        # Load entity state
        state: EntityState | None = (
            await session.execute(
                select(EntityState).where(EntityState.entity == entity)
            )
        ).scalar_one_or_none()
        if not state:
            return None

        # Load recent observations for context
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
        obs = (
            await session.execute(
                select(WorldTimeline)
                .where(
                    WorldTimeline.entity == entity,
                    WorldTimeline.observed_at >= cutoff,
                )
                .order_by(WorldTimeline.observed_at.desc())
                .limit(20)
            )
        ).scalars().all()

        context_lines = [f"{r.observed_at.strftime('%Y-%m-%d %H:%M')} — {r.value}" for r in obs]
        context = "\n".join(context_lines[:10])

        prompt = (
            f"Based on these recent observations for '{entity}':\n{context}\n\n"
            f"Current state: {state.current_value}, trend: {state.trend}"
            f"{f', change: {state.change_pct}%' if state.change_pct else ''}.\n\n"
            f"Provide a brief, confident {horizon} forecast in 1-2 sentences. "
            "Focus on likely direction and key risks. Be concise."
        )

        try:
            resp = await model_manager.generate(
                ModelRequest(
                    messages=[Message(role="user", content=prompt)],
                    max_tokens=150,
                    temperature=0.3,
                )
            )
            prediction = resp.content.strip()
        except Exception as exc:
            logger.warning("world_model.forecast_failed", entity=entity, error=str(exc)[:80])
            return None

        # Persist forecast
        forecast_entry = StatePrediction(
            id=str(uuid4()),
            entity=entity,
            prediction_text=prediction,
            horizon=horizon,
            confidence=0.6,
            model_used=resp.model_used if hasattr(resp, "model_used") else "",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )
        session.add(forecast_entry)
        try:
            await session.commit()
        except Exception:
            await session.rollback()

        return prediction

    # ── Context formatting ────────────────────────────────────────────

    async def format_for_context(
        self, session: AsyncSession, max_entities: int = 6
    ) -> str:
        """Format tracked entity states as a context block for injection."""
        from ..db.models import EntityState
        from sqlalchemy import select

        rows: list[EntityState] = (
            await session.execute(
                select(EntityState)
                .order_by(EntityState.last_updated.desc())
                .limit(max_entities)
            )
        ).scalars().all()

        if not rows:
            return ""

        lines = ["[WORLD MODEL — ESTADO ACTUAL]"]
        _trend_icons = {"up": "📈", "down": "📉", "volatile": "⚡", "stable": "➡️", "unknown": "❓"}
        for row in rows:
            icon = _trend_icons.get(row.trend, "")
            change_str = f" ({row.change_pct:+.1f}%)" if row.change_pct is not None else ""
            lines.append(f"  • {row.entity}: {row.current_value}{change_str} {icon}")

        return "\n".join(lines)
