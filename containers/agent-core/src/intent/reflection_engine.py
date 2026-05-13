"""Execution Self-Reflection Engine — Deterministic quality analysis.

Analyzes _do_track execution traces to:
  1. Build a structured ExecutionAudit
  2. Detect inefficiencies (wasted strategies, slow convergence, unknown selectors)
  3. Identify failure root causes
  4. Generate system-level optimization opportunities
  5. Update StrategyScoreRegistry for adaptive strategy ordering
  6. Feed back into execution_planner.SelectorRegistry after proven successes

Architecture principle:
  NO LLM — all analysis is regex-based and rule-based. Fully deterministic.
  The system observes itself, evaluates itself, and improves itself.
"""
from __future__ import annotations

import re
import time
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ── Default strategy order (baseline before any learning data) ─────────────────
_DEFAULT_STRATEGY_ORDER = [
    "sibling_button_click",
    "enter_key",
    "scrollintoview_click",
    "mouse_event_dispatch",
    "form_submit_js",
    "button_text_discovery",
    "queryselector_js_click",
]


# ── Strategy Score Registry ────────────────────────────────────────────────────

@dataclass
class StrategyScore:
    """Per-domain, per-strategy execution quality score.

    efficiency_score formula:
        success_rate
        - 0.3 * fallback_rate   (penalty for being tried later in the sequence)
        - time_penalty           (penalty for slow execution, normalized to [0..0.2])

    Higher = better — strategies should be sorted descending by efficiency_score.
    """
    strategy: str
    domain: str
    success_count: int = 0
    failure_count: int = 0
    total_time_ms: int = 0
    fallback_position_sum: int = 0  # sum of position indices across all executions
    executions: int = 0             # total times this strategy was tried

    @property
    def total(self) -> int:
        return self.success_count + self.failure_count

    @property
    def success_rate(self) -> float:
        return self.success_count / self.total if self.total > 0 else 0.0

    @property
    def avg_time_ms(self) -> float:
        return self.total_time_ms / self.executions if self.executions > 0 else 0.0

    @property
    def avg_fallback_position(self) -> float:
        """Average position in strategy list when used. 0 = always tried first."""
        return self.fallback_position_sum / self.executions if self.executions > 0 else 3.0

    @property
    def fallback_rate(self) -> float:
        """Fraction of executions where this was not the first strategy tried."""
        return min(self.avg_fallback_position / (len(_DEFAULT_STRATEGY_ORDER) - 1), 1.0)

    @property
    def efficiency_score(self) -> float:
        """Combined quality metric. Range [0.0 .. 1.0]. Higher = better."""
        if self.total < 3:
            return 0.5  # Neutral — not enough data to judge
        sr = self.success_rate
        time_penalty = min(self.avg_time_ms / 150_000.0, 0.2)  # 30s → 0.2 penalty
        return max(0.0, min(1.0, sr - 0.3 * self.fallback_rate - time_penalty))

    def as_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "domain": self.domain,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "total": self.total,
            "success_rate": round(self.success_rate, 3),
            "avg_time_ms": round(self.avg_time_ms),
            "avg_fallback_position": round(self.avg_fallback_position, 2),
            "fallback_rate": round(self.fallback_rate, 3),
            "efficiency_score": round(self.efficiency_score, 3),
        }


# In-memory registries — survive container lifetime, repopulate on restart via Redis later
_STRATEGY_SCORES: dict[str, StrategyScore] = {}          # "{domain}:{strategy}" → StrategyScore
_GLOBAL_STRATEGY_STATS: dict[str, dict] = {}             # strategy → {success, failure, domains}

# ── Adaptive intelligence state ───────────────────────────────────────────────
_STRATEGY_PENALTIES: dict[str, float] = {}  # "{domain}:{strategy}" → extra penalty [0..0.5]
_CONSECUTIVE_FAILURES: dict[str, int] = {}  # "{domain}:{strategy}" → consecutive fail count
_EXPLORATION_LOG: list = []                 # recent exploration events (capped at 100)


def _score_key(domain: str, strategy: str) -> str:
    return f"{domain}:{strategy}"


def _domain_clean(domain: str) -> str:
    return re.sub(r"^www\.", "", (domain or "unknown").lower().strip())


def get_strategy_score(domain: str, strategy: str) -> StrategyScore:
    key = _score_key(_domain_clean(domain), strategy)
    if key not in _STRATEGY_SCORES:
        _STRATEGY_SCORES[key] = StrategyScore(strategy=strategy, domain=_domain_clean(domain))
    return _STRATEGY_SCORES[key]


def _apply_consecutive_penalty(domain: str, strategy: str) -> None:
    """Increment consecutive failure counter and apply escalating penalty."""
    key = _score_key(_domain_clean(domain), strategy)
    _CONSECUTIVE_FAILURES[key] = _CONSECUTIVE_FAILURES.get(key, 0) + 1
    count = _CONSECUTIVE_FAILURES[key]
    # Penalty: 0.05 per consecutive failure, capped at 0.40
    _STRATEGY_PENALTIES[key] = min(0.40, count * 0.05)


def _reset_consecutive_failures(domain: str, strategy: str) -> None:
    """Reset consecutive failure count after a success."""
    key = _score_key(_domain_clean(domain), strategy)
    _CONSECUTIVE_FAILURES.pop(key, None)
    _STRATEGY_PENALTIES.pop(key, None)


def decay_penalties() -> int:
    """Decay all active penalties by 20%. Remove penalties below 0.01.

    Called by OpportunitiesProcessorJob periodically.
    Returns count of penalties decayed.
    """
    to_remove = []
    for key in list(_STRATEGY_PENALTIES):
        _STRATEGY_PENALTIES[key] *= 0.80
        if _STRATEGY_PENALTIES[key] < 0.01:
            to_remove.append(key)
    for key in to_remove:
        del _STRATEGY_PENALTIES[key]
        _CONSECUTIVE_FAILURES.pop(key, None)
    return len(_STRATEGY_PENALTIES)


def classify_failure(trace: str) -> str:
    """Classify a failure trace into a structured failure type.

    Returns one of:
      selector_failure          — input field not found
      submit_failure            — submit action did not trigger DOM change
      result_detection_failure  — submitted but no result text detected
      interference_detected     — dropdown/menu opened instead of submit
      timeout_failure           — page/wait timed out
      navigation_failure        — page did not load
      unknown_failure           — unclassifiable
    """
    t = trace.lower()
    if "no input field found" in t or "locating tracking input" in t and "step 2] failed" in t:
        return "selector_failure"
    if "interference" in t or "dropdown" in t or "menu opened" in t:
        return "interference_detected"
    if "timed out" in t or "timeout" in t:
        return "timeout_failure"
    if "navigat" in t and ("failed" in t or "error" in t) and "step 1" in t:
        return "navigation_failure"
    if "[track_status: failed]" in t or "[form_status: failed]" in t:
        # Had a submit but no result — check if DOM changed at all
        if "dom_changed" in t:
            return "result_detection_failure"
        return "submit_failure"
    if "dom_unchanged" in t and "dom_changed" not in t:
        return "submit_failure"
    return "unknown_failure"


def get_cross_domain_fallback_order() -> list[str]:
    """Return strategy order based on global cross-domain success rates.

    Used as fallback when a domain has insufficient data (< 3 executions per strategy).
    """
    if not _GLOBAL_STRATEGY_STATS:
        return list(_DEFAULT_STRATEGY_ORDER)

    def _global_key(strategy: str) -> tuple[float, int]:
        gs = _GLOBAL_STRATEGY_STATS.get(strategy, {})
        total = gs.get("success", 0) + gs.get("failure", 0)
        if total < 3:
            orig = _DEFAULT_STRATEGY_ORDER.index(strategy) if strategy in _DEFAULT_STRATEGY_ORDER else 99
            return (0.5, orig)
        sr = gs["success"] / total
        return (-sr, 0)

    return sorted(_DEFAULT_STRATEGY_ORDER, key=_global_key)


def get_adaptive_strategy_order(domain: str, epsilon: float = 0.15) -> list[str]:
    """Return strategy order with epsilon-greedy exploration.

    epsilon  (default 0.15 = 15%):
      • With probability (1 - epsilon): exploit → use best known order
      • With probability epsilon: explore → shuffle suboptimal strategies

    Penalties from consecutive failures are applied to efficiency scores.
    Falls back to cross-domain global data when domain data is weak.
    Exploration events are logged to _EXPLORATION_LOG.
    """
    import random

    dc = _domain_clean(domain)
    ordered = get_strategy_order(dc)

    # Apply active penalties to sort key
    def _penalized_key(strategy: str) -> tuple[float, int]:
        key = _score_key(dc, strategy)
        score = _STRATEGY_SCORES.get(key)
        if score is None or score.total < 3:
            # Use cross-domain data if available
            gs = _GLOBAL_STRATEGY_STATS.get(strategy, {})
            gs_total = gs.get("success", 0) + gs.get("failure", 0)
            if gs_total >= 3:
                base = gs["success"] / gs_total
            else:
                orig = _DEFAULT_STRATEGY_ORDER.index(strategy) if strategy in _DEFAULT_STRATEGY_ORDER else 99
                return (0.5 + _STRATEGY_PENALTIES.get(key, 0.0), orig)
        else:
            base = score.efficiency_score
        penalty = _STRATEGY_PENALTIES.get(key, 0.0)
        return (-(base - penalty), 0)

    ordered = sorted(_DEFAULT_STRATEGY_ORDER, key=_penalized_key)

    # Exploitation path (no exploration)
    if random.random() >= epsilon:
        return ordered

    # Exploration path: shuffle strategies ranked 2+ (keep best known at index 0)
    explore_pool = ordered[1:]
    random.shuffle(explore_pool)
    result = [ordered[0]] + explore_pool

    # Log exploration event
    event = {
        "domain": dc,
        "epsilon": epsilon,
        "exploited_first": ordered[0],
        "exploration_order": result,
        "timestamp": time.time(),
    }
    _EXPLORATION_LOG.append(event)
    if len(_EXPLORATION_LOG) > 100:
        _EXPLORATION_LOG.pop(0)
    logger.info(
        "strategy.exploration",
        domain=dc,
        exploited_first=ordered[0],
        exploration_order=result,
    )

    return result


def get_execution_metrics() -> dict:
    """Return aggregated execution intelligence metrics.

    Covers: strategy success rates, exploration activity,
    penalty state, failure type distribution, selector reuse.
    """
    total_executions = sum(v.executions for v in _STRATEGY_SCORES.values())
    total_successes = sum(v.success_count for v in _STRATEGY_SCORES.values())
    total_failures = sum(v.failure_count for v in _STRATEGY_SCORES.values())

    # Per-domain summary
    domains: set[str] = {v.domain for v in _STRATEGY_SCORES.values()}
    domain_summary = {}
    for dc in sorted(domains):
        scores = [v for k, v in _STRATEGY_SCORES.items() if k.startswith(dc + ":")]
        d_exec = sum(s.executions for s in scores)
        d_succ = sum(s.success_count for s in scores)
        best = max(scores, key=lambda s: s.efficiency_score, default=None)
        domain_summary[dc] = {
            "executions": d_exec,
            "success_rate": round(d_succ / d_exec, 3) if d_exec else 0.0,
            "best_strategy": best.strategy if best and best.executions >= 3 else "unknown",
        }

    # Global cross-domain insights
    global_order = get_cross_domain_fallback_order()

    # Exploration stats
    exploration_count = len(_EXPLORATION_LOG)
    recent_explorations = _EXPLORATION_LOG[-5:] if _EXPLORATION_LOG else []

    # Penalty stats
    active_penalties = {k: round(v, 3) for k, v in _STRATEGY_PENALTIES.items() if v > 0.01}

    return {
        "total_executions": total_executions,
        "total_successes": total_successes,
        "total_failures": total_failures,
        "global_success_rate": round(total_successes / total_executions, 3) if total_executions else 0.0,
        "domains_tracked": len(domains),
        "domain_summary": domain_summary,
        "global_strategy_order": global_order,
        "exploration_events": exploration_count,
        "recent_explorations": recent_explorations,
        "active_penalties": active_penalties,
        "consecutive_failure_counts": dict(_CONSECUTIVE_FAILURES),
    }


def get_strategy_order(domain: str) -> list[str]:
    """Return strategies sorted by efficiency score for this domain.

    Rules:
    - Strategy must have ≥3 total executions to participate in reordering.
    - Strategies with insufficient data stay in default position.
    - Ties preserve original default order.
    """
    dc = _domain_clean(domain)

    def _sort_key(strategy: str) -> tuple[float, int]:
        key = _score_key(dc, strategy)
        score = _STRATEGY_SCORES.get(key)
        if score is None or score.total < 3:
            # Not enough data — keep default order by original index
            orig = _DEFAULT_STRATEGY_ORDER.index(strategy) if strategy in _DEFAULT_STRATEGY_ORDER else 99
            return (0.5, orig)   # neutral score, original position as tiebreaker
        return (-score.efficiency_score, 0)  # negative for descending sort, no tiebreaker needed

    return sorted(_DEFAULT_STRATEGY_ORDER, key=_sort_key)


def update_strategy_scores(
    domain: str,
    strategies_tried: list[str],
    winning_strategy: str,
    elapsed_ms: int,
) -> list[str]:
    """Update per-domain and global scores for all strategies used in this execution.

    Returns list of structured log lines tagged [strategy_score_updated].
    """
    dc = _domain_clean(domain)
    logs: list[str] = []

    for position, strategy in enumerate(strategies_tried):
        score = get_strategy_score(dc, strategy)
        score.executions += 1
        score.fallback_position_sum += position

        if strategy == winning_strategy:
            score.success_count += 1
            score.total_time_ms += elapsed_ms
        else:
            score.failure_count += 1

        # Global cross-domain stats
        if strategy not in _GLOBAL_STRATEGY_STATS:
            _GLOBAL_STRATEGY_STATS[strategy] = {
                "success": 0, "failure": 0,
                "domains_won": set(), "domains_tried": set(),
            }
        gs = _GLOBAL_STRATEGY_STATS[strategy]
        gs["domains_tried"].add(dc)
        if strategy == winning_strategy:
            gs["success"] += 1
            gs["domains_won"].add(dc)
        else:
            gs["failure"] += 1

        logs.append(
            f"[strategy_score_updated] domain={dc} strategy={strategy} "
            f"pos={position} success={score.success_count} failure={score.failure_count} "
            f"efficiency={score.efficiency_score:.3f}"
        )

        # Persist to Redis (sync, <1ms — called from worker thread via asyncio.to_thread)
        try:
            from .execution_persistence import persist_strategy_score, persist_global_stat
            persist_strategy_score(dc, strategy, {
                "success_count": score.success_count,
                "failure_count": score.failure_count,
                "total_time_ms": score.total_time_ms,
                "fallback_position_sum": score.fallback_position_sum,
                "executions": score.executions,
            })
            persist_global_stat(strategy, gs)
        except Exception:
            pass  # Persistence must never break the execution flow

    return logs


# ── ExecutionAudit ─────────────────────────────────────────────────────────────

@dataclass
class ExecutionAudit:
    """Complete quality record for one _do_track execution."""
    plan_id: str
    domain: str
    action_type: str
    tracking_code: str
    total_steps: int
    successful_steps: int
    failed_steps: int
    fallback_activations: int           # strategies that failed DOM check
    strategies_tried: list[str]         # unique, in order tried
    winning_strategy: str               # strategy that produced DOM change
    selector_used: str                  # CSS selector used for tracking input
    dom_changes_detected: int           # how many strategies got DOM change confirmation
    result: str                         # "success" | "partial" | "failure"
    total_time_ms: int
    inefficiencies: list[str]
    failure_points: list[str]
    optimization_opportunities: list[str]
    timestamp: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "domain": self.domain,
            "action_type": self.action_type,
            "tracking_code": self.tracking_code,
            "total_steps": self.total_steps,
            "successful_steps": self.successful_steps,
            "failed_steps": self.failed_steps,
            "fallback_activations": self.fallback_activations,
            "strategies_tried": self.strategies_tried,
            "winning_strategy": self.winning_strategy,
            "selector_used": self.selector_used,
            "dom_changes_detected": self.dom_changes_detected,
            "result": self.result,
            "total_time_ms": self.total_time_ms,
            "inefficiencies": self.inefficiencies,
            "failure_points": self.failure_points,
            "optimization_opportunities": self.optimization_opportunities,
            "timestamp": self.timestamp,
        }


# ── Trace parsers ──────────────────────────────────────────────────────────────

_RE_STEP_OK    = re.compile(r"\[Step (\d+)\] OK")
_RE_STEP_FAIL  = re.compile(r"\[Step (\d+)\] FAILED")
_RE_CLICK_ATT  = re.compile(r"\[click_attempt\]\s+strategy=(\S+)")
_RE_DOM_CHNG   = re.compile(r"\[dom_changed\]\s+strategy=(\S+)\s+before=(\d+)\s+after=(\d+)")
_RE_DOM_UNCH   = re.compile(r"\[dom_unchanged\]\s+strategy=(\S+)\s+before=(\d+)\s+after=(\d+)")
_RE_FALLBACK   = re.compile(r"\[fallback_used\]\s+strategy=(\S+)")
_RE_STATUS     = re.compile(r"\[(?:TRACK|FORM)_STATUS:\s*(\w+)\]")
_RE_SELECTOR   = re.compile(
    r"(?:"
    r"\[Step 2\] OK.*?selector=(['\"])([^'\"]+)\1"      # _do_track format
    r"|"
    r"\[field_resolved\].*?selector=(['\"])([^'\"]+)\3" # _do_form_submit format
    r")"
)


def _parse_trace(trace_output: str) -> dict:
    """Extract structured execution data from _do_track trace text."""
    lines = trace_output.split("\n")

    steps_ok   = {int(m.group(1)) for line in lines if (m := _RE_STEP_OK.search(line))}
    steps_fail = {int(m.group(1)) for line in lines if (m := _RE_STEP_FAIL.search(line))}

    # strategies_tried: unique, ordered by first occurrence
    _seen_s: set[str] = set()
    strategies_tried: list[str] = []
    for line in lines:
        m = _RE_CLICK_ATT.search(line)
        if m and m.group(1) not in _seen_s:
            _seen_s.add(m.group(1))
            strategies_tried.append(m.group(1))

    dom_changed_list  = [
        (m.group(1), int(m.group(2)), int(m.group(3)))
        for line in lines if (m := _RE_DOM_CHNG.search(line))
    ]
    dom_unchanged_list = [
        (m.group(1), int(m.group(2)), int(m.group(3)))
        for line in lines if (m := _RE_DOM_UNCH.search(line))
    ]

    winning_strategy = ""
    for line in lines:
        m = _RE_FALLBACK.search(line)
        if m:
            winning_strategy = m.group(1)

    track_status = "unknown"
    for line in lines:
        m = _RE_STATUS.search(line)
        if m:
            track_status = m.group(1).lower()

    selector_used = ""
    for line in lines:
        m = _RE_SELECTOR.search(line)
        if m:
            # group(2) = _do_track "[Step 2] OK" format
            # group(4) = _do_form_submit "[field_resolved]" format
            selector_used = m.group(2) or m.group(4) or ""

    return {
        "steps_ok": steps_ok,
        "steps_fail": steps_fail,
        "strategies_tried": strategies_tried,
        "dom_changed": dom_changed_list,
        "dom_unchanged": dom_unchanged_list,
        "winning_strategy": winning_strategy,
        "track_status": track_status,
        "selector_used": selector_used,
        "lines": lines,
    }


# ── ExecutionReflectionEngine ──────────────────────────────────────────────────

class ExecutionReflectionEngine:
    """Deterministic execution self-analyzer.

    Parses _do_track trace strings and produces ExecutionAudit objects.
    All detection rules are data-driven and regex-based — no LLM.

    The engine observes itself, evaluates itself, improves itself.
    """

    # Strategy must be tried at this position or later to count as "fallback"
    FALLBACK_THRESHOLD_IDX = 1

    # ≥ N dom_unchanged before success = slow convergence inefficiency
    SLOW_DETECTION_MIN = 2

    # Minimum global wins before "should be tried earlier" insight fires
    GLOBAL_WINS_MIN = 3

    def analyze(
        self,
        trace_output: str,
        domain: str,
        action_type: str,
        tracking_code: str,
        plan_id: str,
        total_time_ms: int,
    ) -> ExecutionAudit:
        """Parse trace and build ExecutionAudit. Fully deterministic."""
        from .execution_planner import _SELECTOR_REGISTRY  # avoid circular at module level

        dc = _domain_clean(domain)
        parsed = _parse_trace(trace_output)

        strategies_tried  = parsed["strategies_tried"]
        winning_strategy  = parsed["winning_strategy"]
        dom_changed       = parsed["dom_changed"]
        dom_unchanged     = parsed["dom_unchanged"]
        steps_ok          = parsed["steps_ok"]
        steps_fail        = parsed["steps_fail"]
        track_status      = parsed["track_status"]
        selector_used     = parsed["selector_used"]
        lines             = parsed["lines"]

        total_steps      = len(steps_ok) + len(steps_fail)
        successful_steps = len(steps_ok)
        failed_steps     = len(steps_fail)

        result = (
            "success" if track_status in ("found", "success", "submitted") else
            "partial" if track_status in ("partial",) else
            "failure"
        )

        fallback_activations = len(dom_unchanged)

        # ── Inefficiency detection ─────────────────────────────────────────
        inefficiencies: list[str] = []

        # 1. Winning strategy used late when it has a strong global track record
        if winning_strategy and winning_strategy in strategies_tried:
            win_idx = strategies_tried.index(winning_strategy)
            if win_idx >= self.FALLBACK_THRESHOLD_IDX:
                gs = _GLOBAL_STRATEGY_STATS.get(winning_strategy, {})
                global_wins = gs.get("success", 0)
                if global_wins >= self.GLOBAL_WINS_MIN:
                    inefficiencies.append(
                        f"strategy_order_suboptimal: '{winning_strategy}' tried at position {win_idx} "
                        f"but has {global_wins} global wins — should be tried earlier for {dc}"
                    )

        # 2. Slow submit convergence: many rejected strategies before finding one
        if len(dom_unchanged) >= self.SLOW_DETECTION_MIN:
            inefficiencies.append(
                f"slow_submit_detection: {len(dom_unchanged)} strategies produced no DOM change "
                f"before success — submit method learning needed for {dc}"
            )

        # 3. Generic selector fallback used when site-specific could be registered
        if selector_used and dc not in _SELECTOR_REGISTRY:
            inefficiencies.append(
                f"unknown_selector: no registered selector for {dc}, "
                f"used fallback '{selector_used}' — add to SelectorRegistry after success"
            )

        # 4. Complete strategy exhaustion before success
        if len(strategies_tried) >= len(_DEFAULT_STRATEGY_ORDER) and result == "success":
            inefficiencies.append(
                "full_strategy_exhaustion: all strategies tried before success — "
                f"winning strategy '{winning_strategy}' should be pre-registered for {dc}"
            )

        # ── Failure point analysis ─────────────────────────────────────────
        failure_points: list[str] = []

        if result == "failure":
            for line in lines:
                if "[Step 1] FAILED" in line:
                    detail = line.split("FAILED:", 1)[-1].strip()[:80] if "FAILED:" in line else "navigation error"
                    failure_points.append(f"navigation_failed: {detail}")
                elif "[Step 2] FAILED" in line:
                    failure_points.append("input_not_found: tracking input field not located on page")
                elif "[Step 3] FAILED" in line:
                    failure_points.append("typing_failed: could not enter tracking code into input")
                elif "[Step 4] FAILED" in line:
                    failure_points.append(
                        "submit_exhausted: all 6 strategies produced no DOM change — "
                        "page may block automation or require login"
                    )
                elif "[click_failed] ALL" in line:
                    failure_points.append(
                        "all_strategies_no_dom_change: page did not respond to any submit method"
                    )

            if not failure_points:
                fp_map = {
                    "not_found": "no_tracking_data: page loaded but contained no recognizable tracking status",
                    "failed":    "execution_error: unexpected exception during browser execution",
                    "unknown":   "result_indeterminate: execution completed but TRACK_STATUS not set",
                }
                failure_points.append(fp_map.get(track_status, f"unknown_failure: status={track_status}"))

        # ── Optimization opportunities ─────────────────────────────────────
        optimizations: list[str] = []

        # 1. Promote winning strategy to first position for this domain
        if winning_strategy and winning_strategy in strategies_tried:
            win_idx = strategies_tried.index(winning_strategy)
            if win_idx > 0:
                skipped = strategies_tried[:win_idx]
                optimizations.append(
                    f"promote_strategy: move '{winning_strategy}' to position 0 for {dc} "
                    f"(currently {win_idx}; skipped {skipped} with no DOM change)"
                )

        # 2. Register proven selector for this domain
        if result in ("success", "partial") and selector_used:
            if dc not in _SELECTOR_REGISTRY or "tracking_input" not in _SELECTOR_REGISTRY.get(dc, {}):
                optimizations.append(
                    f"register_selector: '{selector_used}' succeeded for {dc} — "
                    f"add to SelectorRegistry to skip selector discovery on future executions"
                )

        # 3. Global insight: enter_key has low global success rate
        gs_enter = _GLOBAL_STRATEGY_STATS.get("enter_key", {})
        enter_total = gs_enter.get("success", 0) + gs_enter.get("failure", 0)
        if enter_total >= 5:
            enter_sr = gs_enter.get("success", 0) / enter_total
            if enter_sr < 0.20:
                optimizations.append(
                    f"global_insight: enter_key success_rate={enter_sr:.0%} across "
                    f"{enter_total} executions — deprioritize globally; "
                    f"scrollintoview_click has higher empirical success"
                )

        # 4. Too-fast failure: JS may need more render time
        if result == "failure" and total_time_ms < 15_000 and track_status == "not_found":
            optimizations.append(
                "increase_wait_time: execution completed quickly with no result — "
                "JS framework may need more render time; increase Step 5 wait"
            )

        # 5. URL fallback triggered (tried multiple URLs)
        url_fallback_used = any("Retrying with next URL" in line for line in lines)
        if url_fallback_used and result == "success":
            optimizations.append(
                f"register_primary_url: primary URL failed, fallback succeeded for {dc} — "
                f"consider promoting the working URL in the registry"
            )

        audit = ExecutionAudit(
            plan_id=plan_id,
            domain=dc,
            action_type=action_type,
            tracking_code=tracking_code,
            total_steps=total_steps,
            successful_steps=successful_steps,
            failed_steps=failed_steps,
            fallback_activations=fallback_activations,
            strategies_tried=strategies_tried,
            winning_strategy=winning_strategy,
            selector_used=selector_used,
            dom_changes_detected=len(dom_changed),
            result=result,
            total_time_ms=total_time_ms,
            inefficiencies=inefficiencies,
            failure_points=failure_points,
            optimization_opportunities=optimizations,
        )

        # ── Structured logging ─────────────────────────────────────────────
        logger.info(
            "reflection_generated",
            domain=dc, result=result, winning=winning_strategy,
            strategies_tried=len(strategies_tried),
            dom_changes=len(dom_changed), dom_unchanged=len(dom_unchanged),
            time_ms=total_time_ms,
            inefficiencies=len(inefficiencies),
            optimizations=len(optimizations),
        )
        for ineff in inefficiencies:
            logger.info("inefficiency_detected", detail=ineff[:140])
        for opt in optimizations:
            logger.info("optimization_found", detail=opt[:140])

        return audit

    def apply_optimizations(self, audit: ExecutionAudit) -> list[str]:
        """Apply learnable optimizations from audit back to system registries.

        1. Update StrategyScoreRegistry (always)
        2. Register proven selector in SelectorRegistry (if result == success)
        3. Update consecutive failure / penalty state
        4. Record selector outcome for lifecycle tracking
        5. Push failure opportunity to queue (if result == failure)

        Returns list of action log lines.
        """
        from .execution_planner import register_learned_selector, record_selector_outcome

        actions: list[str] = []

        # 1. Update strategy scores for all strategies tried
        if audit.strategies_tried:
            score_logs = update_strategy_scores(
                domain=audit.domain,
                strategies_tried=audit.strategies_tried,
                winning_strategy=audit.winning_strategy,
                elapsed_ms=audit.total_time_ms,
            )
            actions.extend(score_logs)

        # 2. Register proven selector when execution succeeded
        if audit.result == "success" and audit.selector_used:
            from .execution_planner import _SELECTOR_REGISTRY
            dc = _domain_clean(audit.domain)
            existing = _SELECTOR_REGISTRY.get(dc, {})
            _ACTION_INPUT_LABEL = {
                "package_tracking": "tracking_input",
                "web_search":       "search_input",
                "login":            "login_email",
                "form_submit":      "form_input",
            }
            label = _ACTION_INPUT_LABEL.get(audit.action_type, f"{audit.action_type}_input")
            if existing.get(label) is None:
                register_learned_selector(dc, label, audit.selector_used)
                actions.append(
                    f"[optimization_applied] registered selector '{audit.selector_used}' "
                    f"as '{label}' for domain '{dc}'"
                )

        # 3. Update consecutive failure / success state for penalties
        dc = _domain_clean(audit.domain)
        if audit.result == "failure":
            for strategy in audit.strategies_tried:
                _apply_consecutive_penalty(dc, strategy)
            actions.append(
                f"[penalty_applied] domain={dc} strategies={audit.strategies_tried} "
                f"consecutive_failures={[_CONSECUTIVE_FAILURES.get(_score_key(dc, s), 0) for s in audit.strategies_tried]}"
            )
        elif audit.result == "success" and audit.winning_strategy:
            _reset_consecutive_failures(dc, audit.winning_strategy)

        # 4. Update selector health
        if audit.selector_used:
            success = audit.result == "success"
            label = {
                "package_tracking": "tracking_input",
                "web_search":       "search_input",
                "login":            "login_email",
                "form_submit":      "form_input",
            }.get(audit.action_type, f"{audit.action_type}_input")
            record_selector_outcome(dc, label, success)

        # 5. Push opportunity to queue on failure
        if audit.result == "failure":
            try:
                from .execution_persistence import push_opportunity
                failure_type = classify_failure(" ".join(audit.failure_points))
                push_opportunity({
                    "type": "failed_execution_retry",
                    "failure_class": failure_type,
                    "domain": dc,
                    "action_type": audit.action_type,
                    "tracking_code": audit.tracking_code,
                    "strategies_tried": audit.strategies_tried,
                    "failure_points": audit.failure_points[:3],
                    "timestamp": audit.timestamp,
                })
                actions.append(
                    f"[opportunity_queued] type=failed_execution_retry "
                    f"failure_class={failure_type} domain={dc}"
                )
            except Exception as _opp_err:
                pass  # Never break execution flow

        return actions


# ── Module-level singleton ─────────────────────────────────────────────────────
_engine = ExecutionReflectionEngine()


# ── Public API ─────────────────────────────────────────────────────────────────

def reflect_on_execution(
    trace_output: str,
    domain: str,
    action_type: str,
    tracking_code: str,
    plan_id: str,
    total_time_ms: int,
) -> ExecutionAudit:
    """Analyze an execution trace and produce an audit + apply optimizations.

    Works for any action_type (package_tracking, form_submit, web_search, etc.).
    Call after every browser execution — success or failure.
    All work is deterministic; no LLM involved.
    """
    audit = _engine.analyze(
        trace_output, domain, action_type, tracking_code, plan_id, total_time_ms
    )
    _engine.apply_optimizations(audit)
    return audit


def get_optimized_strategy_order(domain: str) -> list[str]:
    """Return adaptive strategy order for a domain (epsilon-greedy).

    15% of the time: explore suboptimal strategies (logged).
    85% of the time: exploit best known order.
    Applies consecutive-failure penalties to scores.
    Falls back to cross-domain global data when domain data is weak.
    """
    return get_adaptive_strategy_order(domain, epsilon=0.15)


def get_strategy_scores_report(domain: str = "") -> dict:
    """Return current strategy scores and global insights.

    Use for debugging, dashboard display, or manual inspection.
    """
    if domain:
        dc = _domain_clean(domain)
        scores = {
            k: v.as_dict()
            for k, v in _STRATEGY_SCORES.items()
            if k.startswith(dc + ":")
        }
    else:
        scores = {k: v.as_dict() for k, v in _STRATEGY_SCORES.items()}

    global_insights = []
    for strategy, gs in _GLOBAL_STRATEGY_STATS.items():
        total = gs.get("success", 0) + gs.get("failure", 0)
        if total > 0:
            sr = gs.get("success", 0) / total
            global_insights.append({
                "strategy": strategy,
                "total_executions": total,
                "success_rate": round(sr, 3),
                "domains_won": sorted(gs.get("domains_won", set())),
                "domains_tried": sorted(gs.get("domains_tried", set())),
            })

    # Sort global insights by success_rate descending
    global_insights.sort(key=lambda x: x["success_rate"], reverse=True)

    return {
        "scores": scores,
        "global_insights": global_insights,
        "strategy_order_sample": {
            dc: get_strategy_order(dc)
            for dc in {v.domain for v in _STRATEGY_SCORES.values()}
        },
    }
