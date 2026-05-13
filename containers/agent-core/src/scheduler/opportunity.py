"""OpportunityEngineJob — scheduler wrapper for the Opportunity Engine.

Registered in main.py as "opportunity_engine" — runs every 7200s (2 hours).

Post-run: promotes high-confidence draft_goal opportunities into real goals via
GoalOrchestrator so the opportunity→goal causal loop is closed.
"""
from __future__ import annotations

import structlog

from ..events.bus import EventBus
from ..models.manager import ModelManager

logger = structlog.get_logger()

# Minimum confidence required to auto-promote an opportunity to a goal
_GOAL_PROMOTION_MIN_CONFIDENCE = 0.80


class OpportunityEngineJob:
    """Scheduler job that runs the Opportunity Engine every 2 hours."""

    def __init__(
        self,
        memory,
        model_manager: ModelManager,
        redis_url: str,
        bus: EventBus,
        notify_chat_id: str = "",
        governor=None,
        goal_orchestrator=None,
    ):
        self.memory = memory
        self.model_manager = model_manager
        self.redis_url = redis_url
        self.bus = bus
        self.notify_chat_id = notify_chat_id
        self.governor = governor
        self.goal_orchestrator = goal_orchestrator

    async def __call__(self) -> str:
        try:
            from ..opportunity_engine import OpportunityEngine
            engine = OpportunityEngine(
                memory=self.memory,
                model_manager=self.model_manager,
                redis_url=self.redis_url,
                bus=self.bus,
                notify_chat_id=self.notify_chat_id,
                governor=self.governor,
            )
            result = await engine.run()
            logger.info("opportunity_engine.job_complete", result=result)

            # Promote high-confidence draft_goal opportunities into real goals
            goals_created = await self._promote_opportunities()
            if goals_created:
                result = f"{result} | goals_promoted={goals_created}"

            return result
        except Exception as exc:
            logger.warning("opportunity_engine.job_failed", error=str(exc)[:200])
            return f"opportunity_engine: error — {str(exc)[:80]}"

    async def _promote_opportunities(self) -> int:
        """Query pending_goal opportunities and create real goals for the high-confidence ones."""
        if not self.goal_orchestrator:
            return 0

        created = 0
        try:
            from ..db.session import async_session
            from ..db.models import Opportunity
            from sqlalchemy import select, update

            async with async_session() as session:
                stmt = (
                    select(Opportunity)
                    .where(Opportunity.status == "pending_goal")
                    .where(Opportunity.action_policy == "draft_goal")
                    .where(Opportunity.confidence >= _GOAL_PROMOTION_MIN_CONFIDENCE)
                    .order_by(Opportunity.confidence.desc())
                    .limit(3)  # Max 3 promoted per run
                )
                rows = (await session.execute(stmt)).scalars().all()

            for opp in rows:
                try:
                    goal = await self.goal_orchestrator.create_goal(
                        objective=opp.description,
                        chat_id=self.notify_chat_id,
                        autonomy_mode=None,
                        priority=4,
                        source="opportunity",
                    )
                    # Mark opportunity as actioned
                    async with async_session() as session:
                        await session.execute(
                            update(Opportunity)
                            .where(Opportunity.id == opp.id)
                            .values(status="goal_created")
                        )
                        await session.commit()
                    created += 1
                    logger.info(
                        "opportunity_engine.goal_promoted",
                        opportunity_id=str(opp.id)[:8],
                        description=opp.description[:80],
                        confidence=opp.confidence,
                        goal_id=goal.id[:8],
                    )
                except Exception as exc:
                    logger.warning(
                        "opportunity_engine.goal_promotion_failed",
                        opportunity_id=str(opp.id)[:8],
                        error=str(exc)[:80],
                    )
        except Exception as exc:
            logger.warning("opportunity_engine.promote_query_failed", error=str(exc)[:80])

        return created
