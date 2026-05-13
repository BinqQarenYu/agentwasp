"""System 6 — Episodic Temporal Reasoning.

Reasons across time using world_timeline data to detect trends and
generate [TEMPORAL INSIGHTS] blocks for LLM context injection.

Architecture:
  world_timeline (database) → TemporalReasoner → TrendSummary list → [TEMPORAL INSIGHTS]

Capabilities:
  1. Entity history retrieval  — last N hours of observations per entity
  2. Delta computation         — current vs. prior value for each entity
  3. Trend classification      — up/down/stable/volatile/unknown
  4. Multi-entity comparison   — rank entities by rate of change
  5. LLM-synthesised narrative — optional LLM insight generation
  6. Context block             — [TEMPORAL INSIGHTS] for build_context()

Feature flag: TEMPORAL_REASONING_ENABLED=true by default.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from ..models.manager import ModelManager

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


class TrendSummary:
    """Summary of temporal trend for a single entity."""

    __slots__ = (
        "entity",
        "current_value",
        "previous_value",
        "change_pct",
        "trend",
        "observations",
        "time_window_hours",
    )

    def __init__(
        self,
        entity: str,
        current_value: str,
        previous_value: str,
        change_pct: float | None,
        trend: str,
        observations: int,
        time_window_hours: float,
    ):
        self.entity = entity
        self.current_value = current_value
        self.previous_value = previous_value
        self.change_pct = change_pct
        self.trend = trend
        self.observations = observations
        self.time_window_hours = time_window_hours

    def __repr__(self) -> str:
        return f"TrendSummary({self.entity!r}, trend={self.trend!r}, change={self.change_pct}%)"


# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------


def _extract_number(text: str) -> float | None:
    """Extract the first numeric value from a string."""
    m = re.search(r"-?[\d]+\.?\d*", text.replace(",", ""))
    if m:
        try:
            return float(m.group())
        except ValueError:
            pass
    return None


def _classify_trend(values: list[float]) -> str:
    if len(values) < 2:
        return "unknown"
    first, last = values[0], values[-1]
    if first == 0:
        return "stable"
    pct = (last - first) / abs(first)
    if pct > 0.05:
        return "up"
    if pct < -0.05:
        return "down"
    avg = sum(values) / len(values)
    variance = sum((v - avg) ** 2 for v in values) / len(values)
    std = math.sqrt(variance)
    if avg and std / avg > 0.04:
        return "volatile"
    return "stable"


# ---------------------------------------------------------------------------
# TemporalReasoner
# ---------------------------------------------------------------------------


class TemporalReasoner:
    """Computes trend summaries from world_timeline and formats context blocks."""

    def __init__(self, max_insights: int = 5):
        self._max_insights = max_insights

    async def get_entity_history(
        self,
        session: AsyncSession,
        entity: str,
        hours: float = 48.0,
    ) -> list[dict]:
        """Return ordered observations for a single entity."""
        from ..db.models import WorldTimeline
        from sqlalchemy import select

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        rows = (
            await session.execute(
                select(WorldTimeline)
                .where(
                    WorldTimeline.entity == entity,
                    WorldTimeline.observed_at >= cutoff,
                )
                .order_by(WorldTimeline.observed_at.asc())
                .limit(200)
            )
        ).scalars().all()

        return [
            {
                "timestamp": row.observed_at.isoformat(),
                "value": row.value,
                "type": row.observation_type,
            }
            for row in rows
        ]

    async def compute_trend(
        self,
        session: AsyncSession,
        entity: str,
        hours: float = 24.0,
    ) -> TrendSummary | None:
        """Compute a TrendSummary for an entity over the last `hours`."""
        history = await self.get_entity_history(session, entity, hours)
        if not history:
            return None

        numeric_values = [
            n
            for h in history
            if (n := _extract_number(h["value"])) is not None
        ]
        trend = _classify_trend(numeric_values) if len(numeric_values) >= 2 else "unknown"

        current_value = history[-1]["value"]
        previous_value = history[0]["value"] if len(history) > 1 else current_value
        change_pct: float | None = None
        if len(numeric_values) >= 2 and numeric_values[0] != 0:
            change_pct = round(
                (numeric_values[-1] - numeric_values[0]) / abs(numeric_values[0]) * 100, 2
            )

        return TrendSummary(
            entity=entity,
            current_value=current_value,
            previous_value=previous_value,
            change_pct=change_pct,
            trend=trend,
            observations=len(history),
            time_window_hours=hours,
        )

    async def compute_all_trends(
        self,
        session: AsyncSession,
        hours: float = 24.0,
        min_observations: int = 2,
    ) -> list[TrendSummary]:
        """Compute trends for all entities active in the last `hours`."""
        from ..db.models import WorldTimeline
        from sqlalchemy import select, distinct

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        entity_rows = (
            await session.execute(
                select(distinct(WorldTimeline.entity)).where(
                    WorldTimeline.observed_at >= cutoff
                )
            )
        ).scalars().all()

        summaries: list[TrendSummary] = []
        for entity in entity_rows:
            if not entity:
                continue
            s = await self.compute_trend(session, entity, hours)
            if s and s.observations >= min_observations:
                summaries.append(s)

        # Sort: highest absolute % change first
        summaries.sort(
            key=lambda s: abs(s.change_pct) if s.change_pct is not None else 0,
            reverse=True,
        )
        return summaries[: self._max_insights]

    # ------------------------------------------------------------------
    # Context formatting
    # ------------------------------------------------------------------

    def format_for_context(self, summaries: list[TrendSummary]) -> str:
        """Format trends as [TEMPORAL INSIGHTS] block for LLM context."""
        if not summaries:
            return ""

        _icons = {
            "up": "📈",
            "down": "📉",
            "volatile": "⚡",
            "stable": "➡️",
            "unknown": "❓",
        }
        lines = ["[TEMPORAL INSIGHTS — CAMBIOS DETECTADOS]"]
        for s in summaries:
            icon = _icons.get(s.trend, "")
            change_str = f" ({s.change_pct:+.1f}%)" if s.change_pct is not None else ""
            lines.append(
                f"  • {s.entity}: {s.current_value}{change_str} {icon} "
                f"[{s.observations} observaciones / {s.time_window_hours:.0f}h]"
            )
        return "\n".join(lines)

    async def build_context_block(
        self,
        session: AsyncSession,
        hours: float = 24.0,
    ) -> str:
        """End-to-end: load trends and return formatted context block."""
        try:
            summaries = await self.compute_all_trends(session, hours)
            return self.format_for_context(summaries)
        except Exception as exc:
            logger.warning("temporal_reasoner.context_error", error=str(exc)[:120])
            return ""
