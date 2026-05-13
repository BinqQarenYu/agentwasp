"""Execution Intelligence Monitor Job — Opportunity Scoring Engine with feedback loop.

Runs every 600 seconds. Two-phase execution:

  Phase 1 — Feedback (runs first):
    Scan pending_eval keys in the 24-72h window.
    For each: compute behavioral outcome by diffing current vs baseline counters.
    Call adapt_signal_weights() so future scoring improves.

  Phase 2 — Detect + Suggest:
    Evaluate all execution evidence with current (post-adaptation) weights.
    Surface SUGGEST-band opportunities to Telegram.
    Record baseline snapshot for future feedback evaluation.

Score bands:
  < 0.30    IGNORE   — discarded
  0.30–0.55 MONITOR  — logged to eim:monitored, not sent to Telegram
  ≥ 0.55    SUGGEST  — sent to Telegram, pending_eval recorded
"""
import structlog

from ..events.bus import EventBus
from ..events.types import EventType
from ..utils.safe_notify import safe_notify

logger = structlog.get_logger()


class ExecutionIntelligenceMonitorJob:
    """Scores execution patterns and self-corrects based on observed behavioral outcomes."""

    def __init__(self, redis_url: str, bus: EventBus, notify_chat_id: str):
        self.redis_url = redis_url
        self.bus = bus
        self.notify_chat_id = str(notify_chat_id)

    # ── Phase 1: Feedback ─────────────────────────────────────────────────────

    async def _run_feedback_phase(self) -> int:
        """Evaluate pending suggestions; adapt weights. Returns count of adaptations."""
        from ..intent.execution_intelligence import (
            evaluate_pending_outcomes,
            adapt_signal_weights,
            log_outcome,
            ExecutionOpportunity,
        )

        adapted = 0
        try:
            evaluations = await evaluate_pending_outcomes(self.redis_url)
            for ev in evaluations:
                try:
                    await adapt_signal_weights(
                        outcome=ev["outcome"],
                        signals=ev["signals"],
                        opp_type=ev["opp_type"],
                        redis_url=self.redis_url,
                    )
                    adapted += 1
                    logger.info(
                        "eim.weight_adapted",
                        opp_type=ev["opp_type"],
                        outcome=ev["outcome"],
                        hours_elapsed=ev.get("hours_elapsed"),
                        signals=ev["signals"],
                    )
                except Exception as exc:
                    logger.warning("eim.adapt_failed", error=str(exc)[:80])
        except Exception as exc:
            logger.warning("eim.feedback_phase_failed", error=str(exc)[:80])

        return adapted

    # ── Phase 2: Detect + Suggest ─────────────────────────────────────────────

    async def __call__(self) -> str:
        from ..intent.execution_intelligence import (
            detect_opportunities,
            mark_opportunity_sent,
            record_pending_evaluation,
            log_outcome,
        )

        if not self.redis_url or not self.notify_chat_id:
            return "eim.skipped: no redis_url or chat_id"

        # Phase 1: evaluate past suggestions first, adapt weights before scoring
        adapted = await self._run_feedback_phase()

        # Phase 2: detect new opportunities with updated weights
        try:
            opportunities = await detect_opportunities(self.redis_url, self.notify_chat_id)
        except Exception as exc:
            logger.warning("eim.detect_failed", error=str(exc)[:80])
            return f"eim.detect_failed: {str(exc)[:80]}"

        sent = 0
        for opp in opportunities:
            try:
                await safe_notify(
                    self.bus,
                    self.notify_chat_id,
                    f"[Execution Intelligence]\n{opp.message}",
                    source="intelligence_monitor",
                )
                await mark_opportunity_sent(opp.fingerprint, self.redis_url)
                await log_outcome(opp, "sent", self.redis_url)

                # Record baseline snapshot so we can evaluate the outcome in 24-72h
                await record_pending_evaluation(opp, self.redis_url)

                sent += 1
                logger.info(
                    "eim.opportunity_sent",
                    opp_type=opp.opp_type,
                    domain=opp.domain,
                    score=f"{opp.score:.2f}",
                    signals=" ".join(f"{k}={v}" for k, v in opp.signals.items()),
                )
            except Exception as exc:
                logger.warning("eim.send_failed", error=str(exc)[:80])

        return f"eim.done: adapted={adapted} detected={len(opportunities)} sent={sent}"
