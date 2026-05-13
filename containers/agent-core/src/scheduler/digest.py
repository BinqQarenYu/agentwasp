"""Wasp Digest — weekly LLM-generated narrative of what the agent has been doing.

Runs daily but generates the narrative only when there's enough new data.
Stored in Redis as `agent:digest` for the overview dashboard to display.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

import structlog

logger = structlog.get_logger()

DIGEST_KEY = "agent:digest"
DIGEST_MIN_INTERVAL_HOURS = 20  # Don't regenerate more than once per 20h


class DigestJob:
    def __init__(self, model_manager, redis_url: str, memory=None):
        self.model_manager = model_manager
        self.redis_url = redis_url
        self.memory = memory

    async def __call__(self) -> None:
        import redis.asyncio as aioredis
        r = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            # Rate-limit: don't regenerate too often
            raw = await r.get(DIGEST_KEY)
            if raw:
                existing = json.loads(raw)
                generated_at = existing.get("generated_at", "")
                if generated_at:
                    last = datetime.fromisoformat(generated_at)
                    hours_ago = (datetime.now(timezone.utc) - last).total_seconds() / 3600
                    if hours_ago < DIGEST_MIN_INTERVAL_HOURS:
                        return

            # Gather stats from various systems
            stats = await self._gather_stats(r)
            # Generate whenever there is any recent activity
            if stats["total_messages_7d"] < 1 and stats.get("procedures_count", 0) < 5:
                return  # Truly empty system, skip

            # Generate narrative
            narrative = await self._generate_narrative(stats)
            if not narrative:
                return

            digest = {
                "text": narrative,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "stats": stats,
            }
            await r.set(DIGEST_KEY, json.dumps(digest))
            logger.info("digest.generated", messages=stats["total_messages_7d"])
        except Exception as e:
            logger.warning("digest.failed", error=str(e))
        finally:
            await r.aclose()

    async def _gather_stats(self, r) -> dict:
        stats = {
            "total_messages_7d": 0,
            "total_skill_calls_7d": 0,
            "top_skills": [],
            "top_kg_entities": [],
            "epistemic_top_domain": "",
            "epistemic_top_confidence": 0.0,
            "dream_count": 0,
            "last_dream_at": "",
            "improvement_queue": [],
            "strengths": [],
            "known_failures_count": 0,
            "procedures_count": 0,
            "timeline_observations_7d": 0,
            "week_label": "",
        }
        now = datetime.now(timezone.utc)
        stats["week_label"] = now.strftime("week of %B %d, %Y")
        cutoff = now - timedelta(days=7)

        # Audit log stats — count all message events (Telegram + dashboard)
        try:
            from ..db.models import AuditLog
            from ..db.session import async_session
            from sqlalchemy import select, func, or_
            async with async_session() as session:
                row = await session.execute(
                    select(func.count(AuditLog.id)).where(
                        or_(
                            AuditLog.event_type == "telegram.message",
                            AuditLog.event_type == "chat.message",
                            AuditLog.event_type == "dashboard.chat",
                        ),
                        AuditLog.timestamp >= cutoff,
                    )
                )
                stats["total_messages_7d"] = row.scalar() or 0
        except Exception:
            pass

        # Self-model stats
        try:
            from ..agent.self_model import load as sm_load
            sm = await sm_load(self.redis_url)
            ws = sm.get("weekly_stats", {})
            stats["total_skill_calls_7d"] = ws.get("skill_calls", 0)
            skills_used = ws.get("skills_used", {})
            stats["top_skills"] = sorted(skills_used.items(), key=lambda x: x[1], reverse=True)[:5]
            stats["dream_count"] = sm.get("dream_count", 0)
            stats["last_dream_at"] = sm.get("last_dream_at", "")
            stats["improvement_queue"] = sm.get("improvement_queue", [])[-3:]
            stats["strengths"] = sm.get("strengths", [])[:4]
            stats["known_failures_count"] = len(sm.get("known_failures", []))

            # Best skill
            rates = sm.get("skill_success_rates", {})
            best_skills = [
                (k, v["success"] / max(1, v["success"] + v["failure"]))
                for k, v in rates.items()
                if v["success"] + v["failure"] >= 3
            ]
            if best_skills:
                best_skill, best_pct = max(best_skills, key=lambda x: x[1])
                stats["best_skill"] = best_skill
                stats["best_skill_pct"] = round(best_pct * 100)
        except Exception:
            pass

        # Epistemic top domain
        try:
            from ..agent.epistemic import load as ep_load
            ep = await ep_load(self.redis_url)
            confs = ep.get("domain_confidence", {})
            if confs:
                top_domain = max(confs, key=confs.get)
                stats["epistemic_top_domain"] = top_domain.replace("_", " ")
                stats["epistemic_top_confidence"] = round(confs[top_domain] * 100)
                # Most improved: compare recent_successes frequency
                recent_s = ep.get("recent_successes", [])
                if recent_s:
                    from collections import Counter
                    top = Counter(recent_s).most_common(1)
                    stats["most_active_domain"] = top[0][0].replace("_", " ") if top else ""
        except Exception:
            pass

        # Top KG entities
        try:
            from ..db.models import KnowledgeNode
            from ..db.session import async_session
            from sqlalchemy import select, desc
            async with async_session() as session:
                result = await session.execute(
                    select(KnowledgeNode.name, KnowledgeNode.entity_type)
                    .order_by(desc(KnowledgeNode.created_at))
                    .limit(10)
                )
                stats["top_kg_entities"] = [
                    {"name": r.name, "type": r.entity_type}
                    for r in result
                ]
        except Exception:
            pass

        # Procedural memory count
        try:
            from ..db.models import ProceduralMemory
            from ..db.session import async_session
            from sqlalchemy import select, func
            async with async_session() as session:
                row = await session.execute(select(func.count(ProceduralMemory.id)))
                stats["procedures_count"] = row.scalar() or 0
        except Exception:
            pass

        # Timeline observations this week
        try:
            from ..db.models import WorldTimeline
            from ..db.session import async_session
            from sqlalchemy import select, func
            async with async_session() as session:
                row = await session.execute(
                    select(func.count(WorldTimeline.id)).where(WorldTimeline.observed_at >= cutoff)
                )
                stats["timeline_observations_7d"] = row.scalar() or 0
        except Exception:
            pass

        return stats

    async def _generate_narrative(self, stats: dict) -> str:
        try:
            from ..models.types import Message, ModelRequest
            prompt = self._build_prompt(stats)
            request = ModelRequest(messages=[
                Message(role="system", content=(
                    "You are Agent Wasp's reflective intelligence. Write a brief, personal, insightful "
                    "weekly digest in the first person. Be concrete with numbers. Show genuine self-awareness. "
                    "Tone: thoughtful, a little proud, always honest. 3-4 short paragraphs max. "
                    "End with one forward-looking sentence about what you expect or plan to focus on next. "
                    "Write in English."
                )),
                Message(role="user", content=prompt),
            ])
            response = await self.model_manager.generate(request)
            return response.content.strip()
        except Exception as e:
            logger.warning("digest.llm_failed", error=str(e))
            return ""

    def _build_prompt(self, s: dict) -> str:
        lines = [f"Generate my weekly digest for {s['week_label']}. Here is my activity data:\n"]
        lines.append(f"- Conversations processed: {s['total_messages_7d']} this week")
        lines.append(f"- Skill calls executed: {s['total_skill_calls_7d']}")
        if s.get("top_skills"):
            skill_str = ", ".join(f"{sk} ({cnt}x)" for sk, cnt in s["top_skills"])
            lines.append(f"- Most used skills: {skill_str}")
        if s.get("best_skill"):
            lines.append(f"- Most reliable skill: {s['best_skill']} at {s.get('best_skill_pct', 0)}% success rate")
        if s.get("epistemic_top_domain"):
            lines.append(f"- Highest confidence domain: {s['epistemic_top_domain']} ({s['epistemic_top_confidence']}%)")
        if s.get("most_active_domain"):
            lines.append(f"- Most active domain this week: {s['most_active_domain']}")
        if s.get("top_kg_entities"):
            entity_str = ", ".join(e["name"] for e in s["top_kg_entities"][:5])
            lines.append(f"- Recent knowledge graph additions: {entity_str}")
        lines.append(f"- Procedural memories stored: {s['procedures_count']} reusable task patterns")
        lines.append(f"- World timeline observations: {s['timeline_observations_7d']} this week")
        lines.append(f"- Dream cycles completed: {s['dream_count']}")
        if s.get("strengths"):
            lines.append(f"- Known strengths: {', '.join(s['strengths'][:3])}")
        if s.get("known_failures_count"):
            lines.append(f"- Failure patterns tracked and avoided: {s['known_failures_count']}")
        if s.get("improvement_queue"):
            lines.append(f"- Active improvement goals: {'; '.join(s['improvement_queue'])}")
        return "\n".join(lines)
