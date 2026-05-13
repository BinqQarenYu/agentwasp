"""Opportunity Scoring Engine — Evidence-based multi-signal scoring with feedback loop.

Evolution from binary rule-based triggers to weighted signal scoring that
self-corrects based on observed behavioral outcomes.

PREVIOUS SYSTEM:
  if count >= 3 AND found >= 1 → trigger
  if fail_rate >= 0.75 AND total >= 5 → trigger

THIS SYSTEM:
  compute_score(signals) → float [0..1]
  score < BAND_IGNORE   → discard
  score < BAND_SUGGEST  → monitor (logged internally, no Telegram)
  score >= BAND_SUGGEST → suggest (Telegram message sent)

  Then: observe behavioral change 24-72h later → update signal weights

Scoring models:

  AUTOMATE_TRACKING score:
    0.40 · frequency_signal   — how many times same code was tracked
    0.35 · success_signal     — did tracking actually produce results?
    0.25 · recency_signal     — was the interest recent? (exponential decay)

  SITE_UNRELIABLE score:
    0.40 · fail_rate_signal   — what % of executions failed?
    0.35 · volume_signal      — how much data do we have?
    0.25 · persistence_signal — has it been failing over time, not just once?

Feedback loop (fully deterministic, no LLM):

  record_pending_evaluation(opp) → stores baseline counter snapshot at send time
  evaluate_pending_outcomes()    → called every 10min; evaluates all pending evals
                                   in the 24–72h window by diffing current vs baseline
  adapt_signal_weights(outcome)  → called for each resolved evaluation

  Outcome detection rules:
    AUTOMATE_TRACKING:
      events_since == 0 → "acted_on"  (stopped tracking manually)
      events_since >= 2 → "ignored"   (kept tracking at same pace)
      events_since == 1 → None        (ambiguous — skip)
    SITE_UNRELIABLE:
      events_since == 0 → "acted_on"  (stopped using the broken site)
      events_since >= 2 → "ignored"   (kept using it anyway)
      events_since == 1 → None        (ambiguous — skip)

  Safeguards:
    - Only evaluate in 24–72h window (too early = no time to act, too late = stale)
    - Skip if current counter key is missing/expired (can't distinguish expired vs acted)
    - Skip if baseline_count is 0 (no stored baseline)
    - Skip if events_since < 0 (data inconsistency)
    - Ambiguous (events_since == 1) never updates weights
    - Weight change rate ±0.02, bounded [0.10, 0.70]

Redis key schema:
  eim:meta:track:{code}          Hash  {count, found, partial, failed, first_ts, last_ts}
  eim:meta:domain:{domain}       Hash  {total, fail, first_ts, last_ts}
  eim:signal_weights             Hash  {signal_name → float}
  eim:pending_eval:{fingerprint} Hash  {opp_type, tracking_code/domain, sent_at,
                                        score, signals, baseline_count/baseline_total}
                                        TTL=72h
  eim:opp_sent:{fingerprint}     SET, TTL=48h   — dedup guard
  eim:daily_count:{date}         INCR, TTL=24h  — max suggestions per day
  eim:outcomes                   List, TTL=30d  — JSON log with signals + scores
  eim:monitored                  List, TTL=7d   — MONITOR band log

Legacy keys (still written for backward compat, not read by scorer):
  eim:track:{code}             INCR, TTL=7d
  eim:track_result:{code}      Hash, TTL=7d
  eim:domain:{domain}:total    INCR, TTL=7d
  eim:domain:{domain}:fail     INCR, TTL=7d

Opportunity types:
  AUTOMATE_TRACKING   — same code tracked repeatedly with results
  SITE_UNRELIABLE     — domain has high failure rate over time
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import structlog
logger = structlog.get_logger()


# ── Score bands ─────────────────────────────────────────────────────────────────

BAND_IGNORE   = 0.30   # Below this → discard entirely
BAND_SUGGEST  = 0.55   # At or above this → send Telegram suggestion

MAX_SUGGESTIONS_DAY = 2
DEDUP_WINDOW_S      = 48 * 3600
TRACK_EVENT_TTL     = 7 * 86400
DAILY_COUNT_TTL     = 86400

# ── Default signal weights ─────────────────────────────────────────────────────
# Stored in Redis under eim:signal_weights — updated by adapt_signal_weights()
_DEFAULT_WEIGHTS: dict[str, float] = {
    # AUTOMATE_TRACKING signals
    "automate_frequency": 0.40,
    "automate_success":   0.35,
    "automate_recency":   0.25,
    # SITE_UNRELIABLE signals
    "unreliable_fail_rate":   0.40,
    "unreliable_volume":      0.35,
    "unreliable_persistence": 0.25,
}

# Adaptation learning rate — small to avoid overreaction to individual events
_WEIGHT_ADAPT_RATE = 0.02
_WEIGHT_MIN = 0.10
_WEIGHT_MAX = 0.70


# ── Redis key builders ─────────────────────────────────────────────────────────

def _k_track_meta(code: str) -> str:
    return f"eim:meta:track:{code.upper()}"

def _k_domain_meta(domain: str) -> str:
    return f"eim:meta:domain:{domain}"

def _k_signal_weights() -> str:
    return "eim:signal_weights"

def _k_opp_sent(fingerprint: str) -> str:
    return f"eim:opp_sent:{fingerprint}"

def _k_daily_count() -> str:
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"eim:daily_count:{day}"

def _k_pending_eval(fingerprint: str) -> str:
    return f"eim:pending_eval:{fingerprint}"

# Legacy keys — still written for backward compat
def _k_track_count(code: str) -> str:
    return f"eim:track:{code.upper()}"

def _k_track_result(code: str) -> str:
    return f"eim:track_result:{code.upper()}"

def _k_domain_total(domain: str) -> str:
    return f"eim:domain:{domain}:total"

def _k_domain_fail(domain: str) -> str:
    return f"eim:domain:{domain}:fail"


# ── Opportunity dataclass ──────────────────────────────────────────────────────

@dataclass
class ExecutionOpportunity:
    """A scored behavioral pattern worth surfacing to the user."""
    opp_type: str           # "AUTOMATE_TRACKING" | "SITE_UNRELIABLE"
    domain: str
    evidence: dict          # raw facts that fed the signals
    score: float            # composite score 0.0–1.0
    signals: dict           # individual signal values for audit/learning
    band: str               # "SUGGEST" | "MONITOR" | "IGNORE"
    message: str            # Telegram message (Spanish)
    confidence: float = 0.0 # backward-compat alias for score
    fingerprint: str = ""
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self):
        self.confidence = self.score
        if not self.fingerprint:
            raw = f"{self.opp_type}:{self.domain}:{self.evidence.get('tracking_code', '')}"
            self.fingerprint = hashlib.md5(raw.encode()).hexdigest()[:16]


# ── Signal computation ─────────────────────────────────────────────────────────

def _compute_automate_score(
    meta: dict,
    weights: dict,
) -> tuple[float, dict]:
    """Compute AUTOMATE_TRACKING score from consolidated meta hash.

    Returns (score, signals_dict). score=0 if insufficient data.
    """
    count     = int(meta.get("count", 0))
    found     = int(meta.get("found", 0))
    partial   = int(meta.get("partial", 0))
    last_ts   = float(meta.get("last_ts", 0))

    if count < 3:
        return 0.0, {}

    # Signal 1: frequency — needs ≥3 events; saturates at 6
    # (count-2)/4: 3→0.25, 4→0.50, 5→0.75, 6→1.0
    freq_s = max(0.0, min(1.0, (count - 2) / 4.0))

    # Signal 2: success — weighted found (partial = 0.5)
    weighted_found = found + partial * 0.5
    success_s = min(1.0, weighted_found / count) if count > 0 else 0.0

    # Signal 3: recency — exponential decay, half-life ~17 hours
    # At 0h: 1.0, at 17h: 0.69, at 34h: 0.48, at 72h: 0.19
    now_ts = time.time()
    age_h = (now_ts - last_ts) / 3600 if last_ts > 0 else 168.0
    recency_s = math.exp(-0.04 * age_h)

    signals = {
        "frequency": round(freq_s, 3),
        "success":   round(success_s, 3),
        "recency":   round(recency_s, 3),
    }

    score = (
        weights.get("automate_frequency", 0.40) * freq_s +
        weights.get("automate_success",   0.35) * success_s +
        weights.get("automate_recency",   0.25) * recency_s
    )

    return round(min(1.0, score), 4), signals


def _compute_unreliable_score(
    meta: dict,
    weights: dict,
) -> tuple[float, dict]:
    """Compute SITE_UNRELIABLE score from consolidated domain meta hash.

    Returns (score, signals_dict). score=0 if insufficient data.
    """
    total    = int(meta.get("total", 0))
    fail     = int(meta.get("fail", 0))
    first_ts = float(meta.get("first_ts", 0))
    last_ts  = float(meta.get("last_ts", 0))

    if total < 3:
        return 0.0, {}

    fail_rate = fail / total

    # Signal 1: fail rate (direct mapping 0→1)
    fail_rate_s = fail_rate

    # Signal 2: volume — saturates at 10 events
    volume_s = min(1.0, total / 10.0)

    # Signal 3: persistence — how long has the domain been failing?
    # span_h = 0: single session; 48h = persistent problem
    span_h = (last_ts - first_ts) / 3600 if (first_ts > 0 and last_ts > 0) else 0.0
    persistence_s = min(1.0, span_h / 48.0)

    signals = {
        "fail_rate":   round(fail_rate_s, 3),
        "volume":      round(volume_s, 3),
        "persistence": round(persistence_s, 3),
    }

    score = (
        weights.get("unreliable_fail_rate",   0.40) * fail_rate_s +
        weights.get("unreliable_volume",      0.35) * volume_s +
        weights.get("unreliable_persistence", 0.25) * persistence_s
    )

    return round(min(1.0, score), 4), signals


def _score_to_band(score: float) -> str:
    if score >= BAND_SUGGEST:
        return "SUGGEST"
    if score >= BAND_IGNORE:
        return "MONITOR"
    return "IGNORE"


# ── Signal weight access ───────────────────────────────────────────────────────

def _load_weights_sync(r) -> dict:
    """Load signal weights from Redis. Falls back to defaults if absent."""
    try:
        raw = r.hgetall(_k_signal_weights())
        w = dict(_DEFAULT_WEIGHTS)
        for k, v in raw.items():
            if k in w:
                try:
                    w[k] = float(v)
                except ValueError:
                    pass
        return w
    except Exception:
        return dict(_DEFAULT_WEIGHTS)


async def _load_weights_async(r) -> dict:
    """Async version of _load_weights_sync."""
    try:
        raw = await r.hgetall(_k_signal_weights())
        w = dict(_DEFAULT_WEIGHTS)
        for k, v in raw.items():
            if k in w:
                try:
                    w[k] = float(v)
                except ValueError:
                    pass
        return w
    except Exception:
        return dict(_DEFAULT_WEIGHTS)


# ── Sync event recorder (called from worker thread) ───────────────────────────

def record_execution_event(
    domain: str,
    tracking_code: str,
    track_status: str,   # "FOUND" | "PARTIAL" | "NOT_FOUND" | "FAILED"
    redis_url: str,
) -> None:
    """Record one execution event. Sync, <2ms. Updates both meta and legacy keys."""
    if not redis_url or not domain:
        return
    try:
        import redis as _r
        r = _r.from_url(redis_url, decode_responses=True)
        now_ts = str(time.time())
        is_fail = track_status in ("NOT_FOUND", "FAILED")

        # ── Meta keys (new — scored by detect_opportunities) ──────────────────
        if tracking_code:
            mk = _k_track_meta(tracking_code)
            r.hincrby(mk, "count", 1)
            r.hset(mk, "last_ts", now_ts)
            if not r.hexists(mk, "first_ts"):
                r.hset(mk, "first_ts", now_ts)
            result_field = track_status.lower() if track_status else "unknown"
            r.hincrby(mk, result_field, 1)
            r.expire(mk, TRACK_EVENT_TTL)

        dmk = _k_domain_meta(domain)
        r.hincrby(dmk, "total", 1)
        r.hset(dmk, "last_ts", now_ts)
        if not r.hexists(dmk, "first_ts"):
            r.hset(dmk, "first_ts", now_ts)
        if is_fail:
            r.hincrby(dmk, "fail", 1)
        r.expire(dmk, TRACK_EVENT_TTL)

        # ── Legacy keys (kept for backward compat) ────────────────────────────
        r.incr(_k_domain_total(domain))
        r.expire(_k_domain_total(domain), TRACK_EVENT_TTL)
        if is_fail:
            r.incr(_k_domain_fail(domain))
            r.expire(_k_domain_fail(domain), TRACK_EVENT_TTL)
        if tracking_code:
            r.incr(_k_track_count(tracking_code))
            r.expire(_k_track_count(tracking_code), TRACK_EVENT_TTL)
            r.hincrby(_k_track_result(tracking_code), result_field, 1)
            r.expire(_k_track_result(tracking_code), TRACK_EVENT_TTL)

        logger.debug(
            "eim.event_recorded",
            domain=domain, code=tracking_code, status=track_status,
        )
    except Exception as exc:
        logger.debug("eim.record_failed", error=str(exc)[:60])


# ── Opportunity detection (async, called by scheduler job) ────────────────────

async def detect_opportunities(redis_url: str, chat_id: str) -> list[ExecutionOpportunity]:
    """Compute scored opportunities from Redis evidence.

    Uses multi-signal scoring to evaluate each candidate.
    Returns only SUGGEST-band opportunities not already sent today.
    MONITOR-band opportunities are logged to outcomes but not returned.
    """
    import redis.asyncio as aioredis

    opportunities: list[ExecutionOpportunity] = []

    try:
        r = aioredis.from_url(redis_url, decode_responses=True)

        # ── Daily limit guard ─────────────────────────────────────────────────
        daily_count = int(await r.get(_k_daily_count()) or 0)
        if daily_count >= MAX_SUGGESTIONS_DAY:
            await r.aclose()
            logger.debug("eim.daily_limit_reached", count=daily_count)
            return []

        # ── Load adaptive weights once ────────────────────────────────────────
        weights = await _load_weights_async(r)

        # ── Scan tracking code candidates ─────────────────────────────────────
        async for key in r.scan_iter("eim:meta:track:*"):
            code = key.split("eim:meta:track:", 1)[-1]
            meta = await r.hgetall(key)
            if not meta:
                continue

            score, signals = _compute_automate_score(meta, weights)
            band = _score_to_band(score)

            if band == "IGNORE":
                continue

            # Success gate: don't suggest automating something that never worked
            found_ok = int(meta.get("found", 0)) + int(meta.get("partial", 0))
            if found_ok == 0:
                continue

            count = int(meta.get("count", 0))
            opp = ExecutionOpportunity(
                opp_type="AUTOMATE_TRACKING",
                domain="",
                evidence={
                    "tracking_code": code,
                    "total_tracks":  count,
                    "found_count":   int(meta.get("found", 0)),
                    "partial_count": int(meta.get("partial", 0)),
                    "failed_count":  int(meta.get("failed", 0)) + int(meta.get("not_found", 0)),
                },
                score=score,
                signals=signals,
                band=band,
                message=_build_automate_message(code, count, found_ok, score),
            )

            if band == "MONITOR":
                # Log internally but don't surface
                await _log_monitor(opp, redis_url)
                continue

            # SUGGEST band — check dedup
            if not await r.get(_k_opp_sent(opp.fingerprint)):
                opportunities.append(opp)

        # ── Scan domain failure candidates ────────────────────────────────────
        async for key in r.scan_iter("eim:meta:domain:*"):
            domain = key.split("eim:meta:domain:", 1)[-1]
            meta = await r.hgetall(key)
            if not meta:
                continue

            score, signals = _compute_unreliable_score(meta, weights)
            band = _score_to_band(score)

            if band == "IGNORE":
                continue

            total = int(meta.get("total", 0))
            fail  = int(meta.get("fail", 0))
            opp = ExecutionOpportunity(
                opp_type="SITE_UNRELIABLE",
                domain=domain,
                evidence={
                    "total_attempts":  total,
                    "failed_attempts": fail,
                    "fail_rate":       round(fail / total, 2) if total > 0 else 0.0,
                },
                score=score,
                signals=signals,
                band=band,
                message=_build_site_failure_message(domain, fail, total, score),
            )

            if band == "MONITOR":
                await _log_monitor(opp, redis_url)
                continue

            if not await r.get(_k_opp_sent(opp.fingerprint)):
                opportunities.append(opp)

        await r.aclose()

    except Exception as exc:
        logger.warning("eim.detect_failed", error=str(exc)[:120])

    # Sort by score descending — highest confidence first
    opportunities.sort(key=lambda o: o.score, reverse=True)
    return opportunities


# ── Signal weight adaptation (feedback loop) ──────────────────────────────────

async def adapt_signal_weights(
    outcome: str,       # "acted_on" | "ignored"
    signals: dict,      # signal values from the opportunity that was sent
    opp_type: str,
    redis_url: str,
) -> None:
    """Adjust signal weights based on outcome. Small online updates.

    Called when we know whether user acted on a suggestion.
    "acted_on": reinforce signals that were strong (≥0.5)
    "ignored":  slightly reduce all weights for this opp_type
    """
    if not redis_url or not signals:
        return
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(redis_url, decode_responses=True)
        weights = await _load_weights_async(r)

        prefix = "automate_" if opp_type == "AUTOMATE_TRACKING" else "unreliable_"
        updates: dict[str, float] = {}
        before: dict[str, float] = {}

        if outcome == "acted_on":
            # Reinforce strong signals — they predicted a good opportunity
            for sig_name, sig_value in signals.items():
                key = f"{prefix}{sig_name}"
                if key in weights and sig_value >= 0.5:
                    before[key] = weights[key]
                    updates[key] = min(_WEIGHT_MAX, weights[key] + _WEIGHT_ADAPT_RATE)
        elif outcome == "ignored":
            # Slightly reduce all weights for this type — was a false positive
            for sig_name in signals:
                key = f"{prefix}{sig_name}"
                if key in weights:
                    before[key] = weights[key]
                    updates[key] = max(_WEIGHT_MIN, weights[key] - _WEIGHT_ADAPT_RATE * 0.5)

        if updates:
            await r.hset(_k_signal_weights(), mapping={k: str(v) for k, v in updates.items()})
            diff = {k: f"{before[k]:.3f}→{updates[k]:.3f}" for k in updates}
            logger.info(
                "[weights_updated]",
                opp_type=opp_type,
                outcome=outcome,
                changes=diff,
            )

        await r.aclose()
    except Exception as exc:
        logger.warning("eim.adapt_weights_failed", error=str(exc)[:60])


# ── Dedup, daily limit, outcome logging ───────────────────────────────────────

async def mark_opportunity_sent(fingerprint: str, redis_url: str) -> None:
    """Mark opportunity as sent. Prevents repeat within 48h."""
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(redis_url, decode_responses=True)
        await r.set(_k_opp_sent(fingerprint), "1", ex=DEDUP_WINDOW_S)
        daily_key = _k_daily_count()
        await r.incr(daily_key)
        await r.expire(daily_key, DAILY_COUNT_TTL)
        await r.aclose()
    except Exception as exc:
        logger.warning("eim.mark_sent_failed", error=str(exc)[:60])


async def log_outcome(opportunity: ExecutionOpportunity, outcome: str, redis_url: str) -> None:
    """Append rich outcome to Redis list. Includes signals for future weight training."""
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(redis_url, decode_responses=True)
        entry = json.dumps({
            "opp_type":   opportunity.opp_type,
            "domain":     opportunity.domain,
            "fingerprint": opportunity.fingerprint,
            "score":      opportunity.score,
            "band":       opportunity.band,
            "signals":    opportunity.signals,
            "evidence":   opportunity.evidence,
            "outcome":    outcome,
            "timestamp":  opportunity.timestamp,
            "sent_at":    time.time(),
        })
        await r.lpush("eim:outcomes", entry)
        await r.ltrim("eim:outcomes", 0, 499)
        await r.expire("eim:outcomes", 30 * 86400)
        await r.aclose()
    except Exception:
        pass


async def _log_monitor(opportunity: ExecutionOpportunity, redis_url: str) -> None:
    """Log MONITOR-band opportunity internally without sending to Telegram."""
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(redis_url, decode_responses=True)
        entry = json.dumps({
            "opp_type":  opportunity.opp_type,
            "domain":    opportunity.domain,
            "score":     opportunity.score,
            "band":      "MONITOR",
            "signals":   opportunity.signals,
            "timestamp": time.time(),
        })
        await r.lpush("eim:monitored", entry)
        await r.ltrim("eim:monitored", 0, 199)
        await r.expire("eim:monitored", 7 * 86400)
        await r.aclose()
    except Exception:
        pass


# ── Message builders ───────────────────────────────────────────────────────────

def _build_automate_message(code: str, total: int, found: int, score: float) -> str:
    confidence_label = "alta" if score >= 0.75 else "media"
    return (
        f"Seguimiento repetido — confianza {confidence_label}\n\n"
        f"Rastreaste el paquete {code} {total} veces "
        f"({found} con resultado).\n\n"
        f"Puedo monitorearlo automáticamente y avisarte cuando cambie el estado.\n"
        f"Escríbeme: monitorea paquete {code} cada 6 horas"
    )


def _build_site_failure_message(domain: str, fails: int, total: int, score: float) -> str:
    pct = int(fails / total * 100) if total > 0 else 0
    confidence_label = "alta" if score >= 0.75 else "media"
    return (
        f"Sitio con problemas — confianza {confidence_label}\n\n"
        f"{domain} ha fallado {fails} de {total} veces ({pct}%).\n\n"
        f"Sugerencia: buscar una alternativa pública para esta tarea "
        f"(la página oficial del carrier, su API, o un sitio agregador distinto)."
    )


# ── Feedback loop ─────────────────────────────────────────────────────────────
# Phase 1: snapshot baseline at suggestion send time
# Phase 2: evaluate behavioral delta 24-72h later
# Phase 3: adapt signal weights based on outcome

# Evaluation timing constants
_EVAL_WINDOW_MIN_H = 24   # Don't evaluate before 24h (user needs time to act)
_EVAL_WINDOW_MAX_H = 72   # Skip after 72h (baselines unreliable, TTL overlap)
_PENDING_EVAL_TTL  = 72 * 3600


async def record_pending_evaluation(
    opp: ExecutionOpportunity,
    redis_url: str,
) -> None:
    """Snapshot baseline counters at suggestion send time.

    Called immediately after a SUGGEST-band opportunity is sent.
    The fingerprint key expires after 72h — after that, evaluation is skipped.
    """
    if not redis_url:
        return
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(redis_url, decode_responses=True)

        mapping: dict[str, str] = {
            "opp_type": opp.opp_type,
            "sent_at":  str(opp.timestamp),
            "score":    str(opp.score),
            "signals":  json.dumps(opp.signals),
        }

        if opp.opp_type == "AUTOMATE_TRACKING":
            mapping["tracking_code"]  = opp.evidence.get("tracking_code", "")
            mapping["baseline_count"] = str(opp.evidence.get("total_tracks", 0))
        elif opp.opp_type == "SITE_UNRELIABLE":
            mapping["domain"]          = opp.domain
            mapping["baseline_total"]  = str(opp.evidence.get("total_attempts", 0))

        key = _k_pending_eval(opp.fingerprint)
        await r.hset(key, mapping=mapping)
        await r.expire(key, _PENDING_EVAL_TTL)
        await r.aclose()

        baseline_display = mapping.get("baseline_count") or mapping.get("baseline_total", "?")
        logger.info(
            "[feedback_pending]",
            fingerprint=opp.fingerprint,
            opp_type=opp.opp_type,
            score=opp.score,
            baseline=baseline_display,
            eval_window="24-72h",
        )
    except Exception as exc:
        logger.warning("eim.pending_eval_record_failed", error=str(exc)[:60])


async def _eval_automate_outcome(r, baseline: dict) -> str | None:
    """Compute AUTOMATE_TRACKING outcome by comparing current vs baseline count.

    Returns "acted_on", "ignored", or None (ambiguous/unevaluable).
    """
    code = baseline.get("tracking_code", "")
    baseline_count = int(baseline.get("baseline_count", 0))

    if not code or baseline_count == 0:
        return None  # missing baseline — can't compare

    meta = await r.hgetall(_k_track_meta(code))
    if not meta:
        # Key expired (TTL=7d) OR data was cleared — can't tell if acted or expired
        return None

    current_count = int(meta.get("count", 0))
    events_since = current_count - baseline_count

    if events_since < 0:
        return None  # data inconsistency (counter reset?)
    elif events_since == 0:
        return "acted_on"   # stopped tracking manually after suggestion
    elif events_since >= 2:
        return "ignored"    # still manually tracking at same pace
    else:
        return None         # exactly 1 new event — ambiguous, don't learn


async def _eval_unreliable_outcome(r, baseline: dict) -> str | None:
    """Compute SITE_UNRELIABLE outcome by comparing current vs baseline total.

    Returns "acted_on", "ignored", or None (ambiguous/unevaluable).
    """
    domain = baseline.get("domain", "")
    baseline_total = int(baseline.get("baseline_total", 0))

    if not domain or baseline_total == 0:
        return None

    dmeta = await r.hgetall(_k_domain_meta(domain))
    if not dmeta:
        return None  # key expired — can't evaluate

    current_total = int(dmeta.get("total", 0))
    events_since = current_total - baseline_total

    if events_since < 0:
        return None
    elif events_since == 0:
        return "acted_on"   # switched away from this domain
    elif events_since >= 2:
        return "ignored"    # kept using the unreliable site
    else:
        return None         # 1 event: ambiguous


async def evaluate_pending_outcomes(redis_url: str) -> list[dict]:
    """Scan pending evaluations in the 24-72h window; compute and return outcomes.

    Called by ExecutionIntelligenceMonitorJob every 10 minutes.
    Returns list of {opp_type, outcome, signals, fingerprint} for weight adaptation.
    Consumes (deletes) each pending_eval key it successfully evaluates.
    """
    import redis.asyncio as aioredis

    results: list[dict] = []
    now = time.time()

    try:
        r = aioredis.from_url(redis_url, decode_responses=True)

        async for key in r.scan_iter("eim:pending_eval:*"):
            try:
                baseline = await r.hgetall(key)
                if not baseline:
                    continue

                sent_at = float(baseline.get("sent_at", 0))
                hours_elapsed = (now - sent_at) / 3600

                if hours_elapsed < _EVAL_WINDOW_MIN_H:
                    continue  # too early — user needs time to act

                if hours_elapsed > _EVAL_WINDOW_MAX_H:
                    # Evaluation window expired — delete without learning
                    await r.delete(key)
                    logger.debug("eim.pending_eval_expired", key=key)
                    continue

                opp_type = baseline.get("opp_type", "")
                signals = json.loads(baseline.get("signals", "{}"))
                fingerprint = key.split("eim:pending_eval:", 1)[-1]

                # Compute behavioral outcome
                outcome: str | None = None
                if opp_type == "AUTOMATE_TRACKING":
                    outcome = await _eval_automate_outcome(r, baseline)
                elif opp_type == "SITE_UNRELIABLE":
                    outcome = await _eval_unreliable_outcome(r, baseline)

                logger.info(
                    "[feedback_evaluated]",
                    fingerprint=fingerprint,
                    opp_type=opp_type,
                    hours_elapsed=round(hours_elapsed, 1),
                    outcome=outcome if outcome else "ambiguous",
                )

                if outcome is not None:
                    results.append({
                        "fingerprint":   fingerprint,
                        "opp_type":      opp_type,
                        "outcome":       outcome,
                        "signals":       signals,
                        "hours_elapsed": round(hours_elapsed, 1),
                    })
                    await r.delete(key)  # consumed
                    logger.info(
                        "[feedback_outcome]",
                        fingerprint=fingerprint,
                        opp_type=opp_type,
                        outcome=outcome,
                        hours_elapsed=round(hours_elapsed, 1),
                        learning="weight_adaptation_queued",
                    )
                # If outcome is None (ambiguous): leave key — re-evaluate on next run

            except Exception as exc:
                logger.debug("eim.eval_key_failed", key=key, error=str(exc)[:60])

        await r.aclose()

    except Exception as exc:
        logger.warning("eim.evaluate_pending_failed", error=str(exc)[:120])

    return results


# ── Public utilities ───────────────────────────────────────────────────────────

def get_outcomes_summary(redis_url: str) -> list[dict]:
    """Return recent opportunity outcomes for dashboard/debugging."""
    try:
        import redis as _r
        r = _r.from_url(redis_url, decode_responses=True)
        raw = r.lrange("eim:outcomes", 0, 49)
        return [json.loads(e) for e in raw]
    except Exception:
        return []


def get_current_weights(redis_url: str) -> dict:
    """Return current signal weights (defaults if never adapted)."""
    try:
        import redis as _r
        r = _r.from_url(redis_url, decode_responses=True)
        return _load_weights_sync(r)
    except Exception:
        return dict(_DEFAULT_WEIGHTS)
