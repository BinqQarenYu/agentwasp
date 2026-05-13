"""Dream Mode — Agent Wasp's background processing during idle hours.

DreamJob runs every hour but only activates when:
  1. The user has been inactive for at least INACTIVITY_THRESHOLD seconds (2h)
  2. AND the current hour is within the configured dream window (default: 1am–7am)

During a dream cycle the agent:
  1. Consolidates recent episodic memories into semantic knowledge
  2. Extracts new entities/relationships from recent conversations for the Knowledge Graph
  3. Runs deep LLM reflection on recent performance and generates improvements
  4. Pre-fetches data the user is likely to request (based on scheduled tasks + patterns)
  5. Updates the Self-Model with reflection results and weekly stats
  6. Logs the cycle to DreamLog table
  7. Analyzes failure patterns from audit_log → stores in self_model["known_failures"] (v2.5)

This gives the agent genuine growth between conversations — not just reactive learning
from explicit feedback, but autonomous processing of its accumulated experience.
"""

import asyncio
import json
import time
import structlog
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from uuid import uuid4

import redis.asyncio as aioredis
from sqlalchemy import select, text

from ..config import now_local
from ..db.models import DreamLog
from ..db.session import async_session
from ..events.bus import EventBus
from ..memory.manager import MemoryManager
from ..memory.types import MemoryQuery, MemoryType
from ..models.manager import ModelManager
from ..models.types import Message, ModelRequest

logger = structlog.get_logger()

INACTIVITY_THRESHOLD = 7200   # 2 hours in seconds
DREAM_WINDOW_START = 1        # 1 AM
DREAM_WINDOW_END = 7          # 7 AM
LAST_ACTIVE_KEY = "agent:last_active"
DREAM_STATE_KEY = "agent:dream_state"

# Failure patterns with ≥ this frequency are flagged high_risk
_HIGH_RISK_THRESHOLD = 3


@dataclass
class FailurePattern:
    """Observed failure pattern extracted from audit_log during a dream cycle.

    Stored in self_model["known_failures"] as observation data only.
    NEVER modifies prompts, rules, or execution paths.
    """
    intent: str        # action / skill name from audit_log.action
    tool: str          # same as intent (tool that failed)
    error_type: str    # "timeout" | "slow_response" | "repeated_failure" | "error"
    frequency: int     # how many times seen in the look-back window
    risk: str          # "high_risk" if frequency >= _HIGH_RISK_THRESHOLD, else "normal"
    sample_error: str  # first 120 chars of error text for context
    first_seen: str    # ISO-8601 timestamp — set once, never reset
    last_seen: str     # ISO-8601 timestamp — updated on every cycle


class DreamJob:
    """Background processing job — runs during quiet/inactive hours."""

    def __init__(
        self,
        memory: MemoryManager,
        model_manager: ModelManager,
        redis_url: str,
        bus: EventBus | None = None,
        notify_chat_id: str = "",
    ):
        self.memory = memory
        self.model_manager = model_manager
        self.redis_url = redis_url
        self.bus = bus
        self.notify_chat_id = notify_chat_id

    async def __call__(self) -> str:
        from ..policy import with_trace
        async with with_trace(
            self.redis_url, path="dream",
            chat_id=self.notify_chat_id, user_text="dream_cycle",
        ) as _trace:
            return await self._run(_trace)

    async def _run(self, _trace=None) -> str:
        def _g(label: str):
            if _trace is not None:
                _trace.add_guard(label)

        # Skip if CPI is high — background processing shouldn't compete with user work
        from ..agent.cpi import is_high as _cpi_high
        if await _cpi_high(self.redis_url):
            logger.info("dream.cpi_throttled")
            _g("dream:cpi_throttled")
            return "Dream skipped: CPI high"

        # Check if dream conditions are met
        r = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            last_active_str = await r.get(LAST_ACTIVE_KEY)
            dream_state = await r.get(DREAM_STATE_KEY)
        finally:
            await r.aclose()

        now = time.time()
        last_active = float(last_active_str) if last_active_str else 0
        inactive_seconds = now - last_active if last_active else INACTIVITY_THRESHOLD + 1
        current_hour = now_local().hour

        # Only dream if user has been inactive AND it's within the dream window
        # Allow dream during any inactive period if it's been >4h (not just night)
        is_night = DREAM_WINDOW_START <= current_hour < DREAM_WINDOW_END
        is_long_inactive = inactive_seconds > INACTIVITY_THRESHOLD * 2  # 4h

        if inactive_seconds < INACTIVITY_THRESHOLD:
            return f"Dream skipped: user active {int(inactive_seconds/60)}m ago"

        if not is_night and not is_long_inactive:
            return f"Dream skipped: not in dream window (hour={current_hour}) and only {int(inactive_seconds/3600)}h inactive"

        # Avoid running dream too frequently — max once per 6h
        if dream_state:
            try:
                state = json.loads(dream_state)
                last_dream = state.get("last_dream", 0)
                if now - last_dream < 21600:  # 6 hours
                    return f"Dream skipped: last dream was {int((now - last_dream)/3600)}h ago"
            except Exception:
                pass

        logger.info("dream.starting", inactive_hours=f"{inactive_seconds/3600:.1f}")
        start_time = time.time()

        results = {
            "memories_consolidated": 0,
            "kg_nodes_added": 0,
            "improvements_proposed": 0,
            "improvements_list": [],
            "reflection": "",
            "failure_patterns_detected": 0,
        }

        # 1. Consolidate recent episodic memories
        try:
            results["memories_consolidated"] = await self._consolidate_memories()
        except Exception:
            logger.exception("dream.consolidate_error")

        # 2. Extract knowledge graph facts from recent conversations
        try:
            results["kg_nodes_added"] = await self._extract_kg_from_history()
        except Exception:
            logger.exception("dream.kg_extract_error")

        # 3. Deep reflection + improvement generation
        try:
            reflection, improvements = await self._deep_reflection()
            results["reflection"] = reflection
            results["improvements_proposed"] = len(improvements)
            results["improvements_list"] = improvements

            # Update self-model
            from ..agent.self_model import update_from_dream
            await update_from_dream(reflection, improvements, self.redis_url)
        except Exception:
            logger.exception("dream.reflection_error")

        # 4. Pre-fetch anticipated data
        try:
            await self._prefetch_anticipated()
        except Exception:
            logger.exception("dream.prefetch_error")

        # 7. v2.5 — Failure pattern analysis (store-only, no behavior change)
        try:
            failure_patterns = await self._analyze_failures()
            results["failure_patterns_detected"] = len(failure_patterns)
        except Exception:
            logger.exception("dream.failure_analysis_error")
            results["failure_patterns_detected"] = 0

        # 5. Log dream cycle to DB
        duration = int(time.time() - start_time)
        try:
            async with async_session() as session:
                log = DreamLog(
                    id=str(uuid4()),
                    duration_seconds=duration,
                    memories_consolidated=results["memories_consolidated"],
                    kg_nodes_added=results["kg_nodes_added"],
                    reflection=results["reflection"][:2000],
                    improvements_proposed=results["improvements_proposed"],
                    improvements_json=results.get("improvements_list", []),
                    prefetch_done=True,
                )
                session.add(log)
                await session.commit()
        except Exception:
            logger.exception("dream.log_error")

        # 6. Update dream state in Redis
        r2 = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            await r2.set(DREAM_STATE_KEY, json.dumps({"last_dream": time.time()}))
        finally:
            await r2.aclose()

        summary = (
            f"Dream cycle complete ({duration}s): "
            f"{results['memories_consolidated']} memories consolidated, "
            f"{results['kg_nodes_added']} KG facts added, "
            f"{results['improvements_proposed']} improvements identified"
        )
        logger.info("dream.complete", **results, duration_s=duration)
        return summary

    async def _consolidate_memories(self) -> int:
        """Promote recent episodic memories to semantic memory."""
        try:
            async with async_session() as session:
                recent = await self.memory.retrieve_recent(
                    session,
                    memory_type=MemoryType.EPISODIC,
                    limit=20,
                )

            # Use PromotionEngine if available
            try:
                from ..memory.promotion import PromotionEngine
                engine = PromotionEngine(self.memory)
                async with async_session() as session:
                    promoted = await engine.run(session)
                return promoted
            except Exception:
                logger.exception("dream.promotion_engine_error")

            return len(recent)
        except Exception:
            return 0

    async def _extract_kg_from_history(self) -> int:
        """Extract knowledge graph entities from recent conversation history."""
        from ..memory.knowledge_graph import extract_from_conversation

        try:
            async with async_session() as session:
                result = await session.execute(
                    text("""
                        SELECT input_summary, output_summary, chat_id
                        FROM audit_log
                        WHERE event_type = 'telegram.message'
                        AND timestamp > NOW() - INTERVAL '24 hours'
                        ORDER BY timestamp DESC
                        LIMIT 30
                    """)
                )
                rows = result.fetchall()

            total_extracted = 0
            for row in rows:
                user_input = row[0] or ""
                agent_output = row[1] or ""
                chat_id = row[2] or ""
                if user_input:
                    n = await extract_from_conversation(
                        user_input, agent_output, chat_id, self.redis_url
                    )
                    total_extracted += n

            return total_extracted
        except Exception:
            logger.exception("dream.kg_extract_error")
            return 0

    async def _deep_reflection(self) -> tuple[str, list[str]]:
        """Run LLM reflection on recent performance. Returns (reflection, improvements)."""
        if not self.model_manager.active_model:
            return "No model available for reflection.", []

        # Gather performance data
        try:
            async with async_session() as session:
                # Recent errors
                errors = await session.execute(
                    text("""
                        SELECT action, error, COUNT(*) as cnt
                        FROM audit_log
                        WHERE error IS NOT NULL
                        AND timestamp > NOW() - INTERVAL '7 days'
                        GROUP BY action, error
                        ORDER BY cnt DESC
                        LIMIT 10
                    """)
                )
                error_rows = errors.fetchall()

                # Most used skills
                skills = await session.execute(
                    text("""
                        SELECT action, COUNT(*) as cnt
                        FROM audit_log
                        WHERE event_type LIKE 'skill.%'
                        AND timestamp > NOW() - INTERVAL '7 days'
                        GROUP BY action
                        ORDER BY cnt DESC
                        LIMIT 10
                    """)
                )
                skill_rows = skills.fetchall()

                # Message count
                msg_count = await session.execute(
                    text("SELECT COUNT(*) FROM audit_log WHERE event_type='telegram.message' AND timestamp > NOW() - INTERVAL '7 days'")
                )
                msg_total = msg_count.scalar() or 0
        except Exception as e:
            return f"Could not gather stats: {e}", []

        error_summary = "\n".join([f"  - {r[0]}: {str(r[1])[:100]} ({r[2]}x)" for r in error_rows]) or "  None"
        skill_summary = "\n".join([f"  - {r[0]}: {r[1]} calls" for r in skill_rows]) or "  None"

        prompt = f"""You are Agent Wasp's internal reflection system. Analyze recent performance and suggest improvements.

STATS (last 7 days):
- Messages processed: {msg_total}
- Top skills used:
{skill_summary}
- Recent errors:
{error_summary}

Based on this data:
1. What patterns do you notice in your performance?
2. What are your 3 most concrete, actionable improvements?
3. What new capability would help you most right now?

Be specific and self-critical. Format as:
REFLECTION: [2-3 sentences of honest self-assessment]
IMPROVEMENTS:
- [specific improvement 1]
- [specific improvement 2]
- [specific improvement 3]"""

        try:
            messages = [
                Message(role="system", content="You are an AI agent performing honest self-reflection."),
                Message(role="user", content=prompt),
            ]
            from ..models.types import ModelRequest
            response = await self.model_manager.generate(ModelRequest(messages=messages))
            content = response.content.strip()

            # Parse reflection and improvements
            reflection = ""
            improvements = []

            lines = content.split("\n")
            in_improvements = False
            for line in lines:
                if line.startswith("REFLECTION:"):
                    reflection = line.replace("REFLECTION:", "").strip()
                elif line.startswith("IMPROVEMENTS:"):
                    in_improvements = True
                elif in_improvements and line.strip().startswith("-"):
                    imp = line.strip().lstrip("- ").strip()
                    if imp:
                        improvements.append(imp)

            if not reflection:
                reflection = content[:300]

            return reflection, improvements[:5]
        except Exception as e:
            logger.exception("dream.reflection_llm_error")
            return f"Reflection failed: {e}", []

    async def _prefetch_anticipated(self) -> None:
        """Pre-fetch data that the user is likely to request based on active tasks."""
        try:
            r = aioredis.from_url(self.redis_url, decode_responses=True)
            tasks_raw = await r.hgetall("custom_tasks")
            await r.aclose()

            for task_id, task_json in tasks_raw.items():
                try:
                    task = json.loads(task_json)
                    instruction = task.get("instruction", "").lower()

                    # Pre-fetch crypto prices for tasks that request them
                    symbols = []
                    for sym in ["BTC", "ETH", "SOL", "BNB", "ADA"]:
                        if sym.lower() in instruction or sym in instruction:
                            symbols.append(sym)

                    if symbols:
                        import httpx
                        from ..memory.temporal import record_observation as _record_obs
                        async with httpx.AsyncClient(timeout=5) as client:
                            for sym in symbols[:3]:
                                try:
                                    resp = await client.get(
                                        f"https://api.coinbase.com/v2/prices/{sym}-USD/spot"
                                    )
                                    if resp.status_code == 200:
                                        data = resp.json()
                                        price = data.get("data", {}).get("amount", "?")
                                        logger.info("dream.prefetch_price", symbol=sym, price=price)
                                        # Store in temporal model for agent context
                                        await _record_obs(
                                            entity=sym,
                                            observation_type="price",
                                            value=f"${price}",
                                            source="dream_prefetch",
                                            expires_hours=6,
                                        )
                                except Exception:
                                    pass
                except Exception:
                    continue
        except Exception:
            pass

    async def _analyze_failures(self) -> list[FailurePattern]:
        """Extract failure patterns from audit_log and store in self_model.

        STORE ONLY — never modifies prompts, rules, or execution paths.
        Results are written to self_model["known_failures"] as structured
        FailurePattern observations for future inspection.

        Look-back window: 7 days (matches _deep_reflection query window).
        """
        patterns: list[FailurePattern] = []
        try:
            async with async_session() as session:
                rows = await session.execute(
                    text("""
                        SELECT
                            action,
                            error,
                            COUNT(*)        AS frequency,
                            AVG(latency_ms) AS avg_latency_ms
                        FROM audit_log
                        WHERE error IS NOT NULL
                          AND error != ''
                          AND timestamp > NOW() - INTERVAL '7 days'
                        GROUP BY action, error
                        ORDER BY frequency DESC
                        LIMIT 20
                    """)
                )
                failure_rows = rows.fetchall()
        except Exception as exc:
            logger.debug("dream.failure_analysis_query_error error=%r", str(exc)[:80])
            return patterns

        for row in failure_rows:
            action      = str(row[0] or "unknown")
            error_text  = str(row[1] or "")
            frequency   = int(row[2] or 0)
            avg_latency = float(row[3] or 0.0)

            # Classify error type — explicit timeout keyword takes priority
            err_lower = error_text.lower()
            now_iso = datetime.now(timezone.utc).isoformat()
            if "timeout" in err_lower:
                error_type = "timeout"
            elif avg_latency > 5000:
                error_type = "slow_response"
            elif frequency >= _HIGH_RISK_THRESHOLD:
                error_type = "repeated_failure"
            else:
                error_type = "error"

            risk = "high_risk" if frequency >= _HIGH_RISK_THRESHOLD else "normal"

            fp = FailurePattern(
                intent=action,
                tool=action,
                error_type=error_type,
                frequency=frequency,
                risk=risk,
                sample_error=error_text[:120],
                first_seen=now_iso,
                last_seen=now_iso,
            )
            patterns.append(fp)

            logger.info(
                "dream_failure_pattern_detected",
                tool=action,
                error_type=error_type,
                frequency=frequency,
                risk=risk,
            )

        if patterns:
            try:
                from ..agent.self_model import load as _sm_load, save as _sm_save
                model = await _sm_load(self.redis_url)
                failures = model.setdefault("known_failures", [])
                now_iso = datetime.now(timezone.utc).isoformat()

                for fp in patterns:
                    # Dedup key: (tool, error_type)
                    existing = next(
                        (e for e in failures
                         if e.get("tool") == fp.tool and e.get("error_type") == fp.error_type),
                        None,
                    )
                    if existing:
                        # Backward compat: old entries may lack first_seen / last_seen
                        if not existing.get("first_seen"):
                            existing["first_seen"] = now_iso
                        # Update mutable fields; first_seen is never reset
                        existing["last_seen"]  = now_iso
                        existing["frequency"]  = fp.frequency
                        existing["risk"]       = fp.risk
                        existing["sample_error"] = fp.sample_error
                        existing.pop("pattern", None)   # remove legacy decorative field
                        existing.pop("observed_at", None)  # replaced by last_seen
                        logger.info(
                            "dream_failure_pattern_updated",
                            tool=fp.tool,
                            error_type=fp.error_type,
                            frequency=fp.frequency,
                            risk=fp.risk,
                        )
                    else:
                        entry = {
                            "tool":         fp.tool,
                            "error_type":   fp.error_type,
                            "frequency":    fp.frequency,
                            "risk":         fp.risk,
                            "sample_error": fp.sample_error,
                            "solution":     "",      # intentionally blank — observation only
                            "first_seen":   fp.first_seen,
                            "last_seen":    fp.last_seen,
                        }
                        failures.append(entry)

                # Keep at most 30 entries (oldest drop first)
                model["known_failures"] = failures[-30:]
                await _sm_save(model, self.redis_url)
                logger.info(
                    "dream.failure_patterns_stored",
                    count=len(patterns),
                    high_risk=sum(1 for p in patterns if p.risk == "high_risk"),
                )
            except Exception as exc:
                logger.debug("dream.failure_store_error error=%r", str(exc)[:80])

        return patterns
