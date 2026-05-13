"""Agent identity helpers — birth date and experience points."""
from __future__ import annotations

import math
from datetime import datetime, timezone

from sqlalchemy import update

from ..db.models import AgentIdentity
from ..db.session import async_session


# ── Level formula ────────────────────────────────────────────────────────────
# XP required to reach level N from level N-1 = N * 100
#   Level 1→2:  100 XP
#   Level 2→3:  200 XP
#   Level 3→4:  300 XP … grows with level
# Total XP at start of level N = sum(i=1..N-1) i*100 = N*(N-1)/2 * 100

def _xp_at_level_start(level: int) -> int:
    """Total XP accumulated at the start of a given level (1-indexed)."""
    return level * (level - 1) // 2 * 100


def compute_level(xp: int) -> dict:
    """Return level info dict from raw XP total.

    level      — current level (starts at 1)
    xp_progress — XP earned within this level
    xp_needed   — XP required to complete this level (grows each level)
    xp_next     — total XP threshold for next level
    pct         — 0-100 progress percentage within this level
    """
    if xp <= 0:
        return {"level": 1, "xp_progress": 0, "xp_needed": 100, "xp_next": 100, "pct": 0}
    # Solve level*(level-1)/2 * 100 <= xp  →  level ≈ (1 + sqrt(1 + 8*xp/100)) / 2
    level = max(1, int((1 + math.sqrt(1 + 8 * xp / 100)) / 2))
    # Clamp to actual value (float rounding safety)
    while _xp_at_level_start(level + 1) <= xp:
        level += 1
    while level > 1 and _xp_at_level_start(level) > xp:
        level -= 1

    xp_start    = _xp_at_level_start(level)
    xp_next     = _xp_at_level_start(level + 1)
    xp_needed   = xp_next - xp_start          # = level * 100
    xp_progress = xp - xp_start
    pct         = min(100, int(xp_progress * 100 / xp_needed)) if xp_needed else 100
    return {
        "level":        level,
        "xp_progress":  xp_progress,
        "xp_needed":    xp_needed,
        "xp_next":      xp_next,
        "pct":          pct,
    }


def _age_label(age_days: int) -> dict:
    """Break age_days into years/months/days and a display mode."""
    years  = age_days // 365
    rem    = age_days % 365
    months = rem // 30
    days   = rem % 30
    if years > 0:
        mode = "ymd"
    elif months > 0:
        mode = "md"
    else:
        mode = "d"
    return {"years": years, "months": months, "days": days, "mode": mode, "total_days": age_days}


async def get_or_create() -> AgentIdentity:
    """Return the singleton AgentIdentity row, creating it if missing."""
    async with async_session() as session:
        row = await session.get(AgentIdentity, 1)
        if row is None:
            row = AgentIdentity(id=1, born_at=datetime.now(timezone.utc), total_xp=0)
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return row


async def add_xp(amount: int = 1) -> None:
    """Increment total_xp atomically."""
    async with async_session() as session:
        await session.execute(
            update(AgentIdentity)
            .where(AgentIdentity.id == 1)
            .values(total_xp=AgentIdentity.total_xp + amount)
        )
        await session.commit()


async def get_identity() -> dict:
    """Return full identity dict for dashboard use."""
    row = await get_or_create()
    now  = datetime.now(timezone.utc)
    born = row.born_at if row.born_at.tzinfo else row.born_at.replace(tzinfo=timezone.utc)
    age_days = (now - born).days
    xp       = row.total_xp or 0
    return {
        "born_at":  born.isoformat(),
        "total_xp": xp,
        "age_days": age_days,
        "age":      _age_label(age_days),
        "lv":       compute_level(xp),
    }
