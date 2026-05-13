"""Opportunities Processor Job — Consumes the opportunities:pending queue.

Runs every 300 seconds. Processes up to 10 queued execution improvement
opportunities per run. Each opportunity triggers a targeted system update:

  failed_execution_retry      → decays penalties, logs for retry tracking
  selector_improvement        → marks selector weak, pushes improvement signal
  strategy_reordering         → forces re-evaluation of strategy order
  anomaly_detection           → logs anomaly, optionally triggers EIM check

Also performs periodic penalty decay on strategy scores and emits structured
metrics to aid observability.
"""
import time
import structlog

logger = structlog.get_logger()


class OpportunitiesProcessorJob:
    """Periodic consumer for execution improvement opportunities."""

    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self._runs = 0
        self._total_processed = 0

    async def __call__(self) -> str:
        self._runs += 1
        if not self.redis_url:
            return "opportunities_processor.skipped: no redis_url"

        from ..intent.execution_persistence import (
            pop_opportunities,
            get_opportunities_queue_depth,
        )
        from ..intent.reflection_engine import (
            decay_penalties,
            get_execution_metrics,
            _STRATEGY_PENALTIES,
        )

        # ── 1. Check queue depth before processing ────────────────────────────
        queue_depth = await get_opportunities_queue_depth(self.redis_url)

        # ── 2. Pop and process opportunities ─────────────────────────────────
        opportunities = await pop_opportunities(self.redis_url, max_count=10)
        processed = 0
        type_counts: dict[str, int] = {}

        for opp in opportunities:
            opp_type = opp.get("type", "unknown")
            type_counts[opp_type] = type_counts.get(opp_type, 0) + 1
            try:
                await self._process_opportunity(opp)
                processed += 1
            except Exception as exc:
                logger.warning(
                    "opportunities_processor.process_failed",
                    opp_type=opp_type,
                    domain=opp.get("domain", ""),
                    error=str(exc)[:80],
                )

        self._total_processed += processed

        # ── 3. Periodic penalty decay (every run) ────────────────────────────
        remaining_penalties = decay_penalties()

        # ── 4. Emit structured metrics every 3 runs ───────────────────────────
        if self._runs % 3 == 0:
            metrics = get_execution_metrics()
            logger.info(
                "execution.metrics_snapshot",
                total_executions=metrics["total_executions"],
                global_success_rate=metrics["global_success_rate"],
                domains_tracked=metrics["domains_tracked"],
                exploration_events=metrics["exploration_events"],
                active_penalties=len(metrics["active_penalties"]),
                processor_total_processed=self._total_processed,
            )

        summary = (
            f"opportunities_processor.done: "
            f"queue_depth={queue_depth} processed={processed} "
            f"types={type_counts} penalties_remaining={remaining_penalties}"
        )
        logger.info("opportunities_processor.run", **{
            "queue_depth": queue_depth,
            "processed": processed,
            "type_counts": type_counts,
            "penalties_remaining": remaining_penalties,
            "run": self._runs,
        })
        return summary

    async def _process_opportunity(self, opp: dict) -> None:
        """Dispatch processing for a single opportunity by type."""
        opp_type = opp.get("type", "unknown")
        domain = opp.get("domain", "")
        timestamp = opp.get("timestamp", 0)
        age_s = time.time() - timestamp if timestamp else 0

        if opp_type == "failed_execution_retry":
            await self._handle_failed_execution(opp, domain, age_s)
        elif opp_type == "selector_improvement":
            await self._handle_selector_improvement(opp, domain)
        elif opp_type == "strategy_reordering":
            await self._handle_strategy_reordering(opp, domain)
        elif opp_type == "anomaly_detection":
            await self._handle_anomaly(opp, domain)
        else:
            logger.info(
                "opportunities_processor.unknown_type",
                opp_type=opp_type,
                domain=domain,
            )

    async def _handle_failed_execution(self, opp: dict, domain: str, age_s: float) -> None:
        """Process a failed execution opportunity.

        Actions:
        - Log structured failure analysis
        - Push selector_improvement opportunity if failure was selector-related
        - Log anomaly if failure rate for domain exceeds threshold
        """
        failure_class = opp.get("failure_class", "unknown_failure")
        strategies_tried = opp.get("strategies_tried", [])
        action_type = opp.get("action_type", "")

        logger.info(
            "opportunities_processor.failed_execution",
            domain=domain,
            failure_class=failure_class,
            action_type=action_type,
            strategies_tried=strategies_tried,
            age_s=round(age_s),
        )

        # If selector failure — generate a selector_improvement opportunity
        if failure_class == "selector_failure" and domain:
            from ..intent.execution_persistence import push_opportunity
            push_opportunity({
                "type": "selector_improvement",
                "domain": domain,
                "action_type": action_type,
                "reason": "selector_failure_detected",
                "timestamp": time.time(),
            })
            logger.info(
                "opportunities_processor.selector_improvement_queued",
                domain=domain,
                action_type=action_type,
            )

        # Check for anomaly: if we have recent failures on this domain
        await self._check_domain_failure_anomaly(domain, failure_class)

    async def _handle_selector_improvement(self, opp: dict, domain: str) -> None:
        """Process a selector improvement opportunity.

        Actions:
        - Log selector weakness for the domain
        - Check current selector confidence scores
        - If confidence < 0.3, generate strategy_reordering opportunity
        """
        action_type = opp.get("action_type", "")
        reason = opp.get("reason", "")

        label_map = {
            "package_tracking": "tracking_input",
            "form_submit":       "form_input",
        }
        element_type = label_map.get(action_type, "tracking_input")

        try:
            from ..intent.execution_planner import get_selector_confidence
            confidence = get_selector_confidence(domain, element_type)
            logger.info(
                "opportunities_processor.selector_health_check",
                domain=domain,
                element_type=element_type,
                confidence=round(confidence, 3),
                reason=reason,
            )

            # If very low confidence → trigger strategy reordering
            if confidence < 0.3:
                from ..intent.execution_persistence import push_opportunity
                push_opportunity({
                    "type": "strategy_reordering",
                    "domain": domain,
                    "action_type": action_type,
                    "reason": f"low_selector_confidence_{confidence:.2f}",
                    "timestamp": time.time(),
                })
        except Exception as exc:
            logger.warning(
                "opportunities_processor.selector_check_failed",
                domain=domain, error=str(exc)[:80],
            )

    async def _handle_strategy_reordering(self, opp: dict, domain: str) -> None:
        """Process a strategy reordering opportunity.

        Actions:
        - Log current strategy order for the domain
        - Emit cross-domain fallback order for comparison
        """
        try:
            from ..intent.reflection_engine import (
                get_strategy_order,
                get_cross_domain_fallback_order,
                get_adaptive_strategy_order,
            )
            current_order = get_strategy_order(domain)
            global_order = get_cross_domain_fallback_order()
            adaptive_order = get_adaptive_strategy_order(domain, epsilon=0.0)  # pure exploit

            logger.info(
                "opportunities_processor.strategy_reorder",
                domain=domain,
                reason=opp.get("reason", ""),
                current_order=current_order,
                global_fallback_order=global_order,
                adaptive_order=adaptive_order,
            )
        except Exception as exc:
            logger.warning(
                "opportunities_processor.reorder_failed",
                domain=domain, error=str(exc)[:80],
            )

    async def _handle_anomaly(self, opp: dict, domain: str) -> None:
        """Process an anomaly detection opportunity."""
        anomaly_type = opp.get("anomaly_type", "unknown")
        details = opp.get("details", {})
        logger.warning(
            "opportunities_processor.anomaly",
            domain=domain,
            anomaly_type=anomaly_type,
            details=details,
        )

    async def _check_domain_failure_anomaly(self, domain: str, failure_class: str) -> None:
        """Check if domain has an anomalous failure rate and push anomaly opportunity."""
        if not domain or not self.redis_url:
            return
        try:
            import redis.asyncio as _aioredis
            r = await _aioredis.from_url(self.redis_url)
            try:
                # Use EIM meta data to check failure rate
                meta = await r.hgetall(f"eim:meta:domain:{domain}")
                if not meta:
                    return
                total = int(meta.get(b"total", 0))
                fail = int(meta.get(b"fail", 0))
                if total >= 5 and fail / total > 0.6:
                    from ..intent.execution_persistence import push_opportunity
                    push_opportunity({
                        "type": "anomaly_detection",
                        "domain": domain,
                        "anomaly_type": "high_failure_rate",
                        "details": {
                            "total": total,
                            "fail": fail,
                            "rate": round(fail / total, 3),
                            "failure_class": failure_class,
                        },
                        "timestamp": time.time(),
                    })
                    logger.warning(
                        "opportunities_processor.anomaly_queued",
                        domain=domain,
                        fail_rate=round(fail / total, 3),
                        total=total,
                    )
            finally:
                await r.aclose()
        except Exception:
            pass
