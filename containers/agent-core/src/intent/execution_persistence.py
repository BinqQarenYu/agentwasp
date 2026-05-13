"""Execution Knowledge Persistence Layer.

Three-tier storage model:
  L1  In-memory dicts (reflection_engine._STRATEGY_SCORES etc.) — instant
  L2  Redis Hashes  — sub-millisecond, runtime persistence across restarts
  L3  PostgreSQL    — crash-durable, long-term audit trail

Write path (called from sync worker thread — safe to use sync redis):
  1. In-memory already updated by caller
  2. Redis HSET — synchronous, <1 ms
  3. PostgreSQL — handled by ExecutionKnowledgeSyncJob (every 300 s)

Load path (async, called once at startup in main.py):
  1. SELECT all rows from PostgreSQL → authoritative source
  2. Merge any Redis keys not yet synced (handles crash between write + pg sync)
  3. Populate in-memory structures in reflection_engine + execution_planner

Redis key schema:
  execution:strategy_scores  Hash  {"{domain}:{strategy}" → JSON}
  execution:selectors        Hash  {"{domain}:{element_type}" → selector}
  execution:global_stats     Hash  {strategy_name → JSON}

PostgreSQL:
  execution_knowledge table — (key_type, domain, name, data JSONB)
  Unique on (key_type, domain, name) — upsert on conflict.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import redis as _sync_redis

logger = logging.getLogger(__name__)

# Set by configure_persistence() at startup — must be called before any write
_redis_url: str = ""


def configure_persistence(redis_url: str) -> None:
    """Store redis_url so sync write helpers can connect. Call once at startup."""
    global _redis_url
    _redis_url = redis_url


def _sync_redis_client():
    """Return a sync redis client. Raises if not configured."""
    if not _redis_url:
        raise RuntimeError("execution_persistence not configured — call configure_persistence(url) at startup")
    return _sync_redis.from_url(_redis_url, decode_responses=True)


# ── Redis key constants ────────────────────────────────────────────────────────

_KEY_SCORES          = "execution:strategy_scores"
_KEY_SELECTORS       = "execution:selectors"
_KEY_GLOBALS         = "execution:global_stats"
_KEY_SIGNAL_WEIGHTS  = "eim:signal_weights"   # EIM feedback-loop learned weights
_KEY_OPPORTUNITIES   = "opportunities:pending"  # LPUSH producer / RPOP consumer
_KEY_METRICS_LOG     = "execution:metrics_log"  # Recent metrics snapshots (list, cap 50)

_OPPORTUNITIES_MAX   = 500   # hard cap — prevent unbounded growth
_OPPORTUNITIES_TTL   = 86400 * 7  # 7 days expiry on list itself


# ── Sync write helpers (called from worker threads) ───────────────────────────


def push_opportunity(opp_dict: dict) -> None:
    """Push one execution improvement opportunity to the pending queue.

    Called synchronously from worker threads (browser execution path).
    Never raises — opportunity loss is preferable to execution failure.
    """
    if not _redis_url:
        return
    try:
        r = _sync_redis_client()
        # Trim list to prevent unbounded growth before pushing
        current_len = r.llen(_KEY_OPPORTUNITIES)
        if current_len >= _OPPORTUNITIES_MAX:
            r.ltrim(_KEY_OPPORTUNITIES, 0, _OPPORTUNITIES_MAX - 2)
        r.lpush(_KEY_OPPORTUNITIES, json.dumps(opp_dict))
        r.expire(_KEY_OPPORTUNITIES, _OPPORTUNITIES_TTL)
    except Exception as exc:
        logger.warning("execution_persistence.push_opportunity_failed", error=str(exc)[:80])

def persist_strategy_score(domain: str, strategy: str, score_data: dict) -> None:
    """Write one StrategyScore to Redis. Called from update_strategy_scores()."""
    if not _redis_url:
        return
    try:
        r = _sync_redis_client()
        r.hset(_KEY_SCORES, f"{domain}:{strategy}", json.dumps(score_data))
    except Exception as exc:
        logger.warning("execution_persistence.strategy_write_failed", error=str(exc)[:80])


def persist_selector(domain: str, element_type: str, selector: str) -> None:
    """Write one learned selector to Redis. Called from register_learned_selector()."""
    if not _redis_url:
        return
    try:
        r = _sync_redis_client()
        r.hset(_KEY_SELECTORS, f"{domain}:{element_type}", selector)
    except Exception as exc:
        logger.warning("execution_persistence.selector_write_failed", error=str(exc)[:80])


def persist_global_stat(strategy: str, stat_data: dict) -> None:
    """Write one global strategy stat to Redis. Called from update_strategy_scores()."""
    if not _redis_url:
        return
    try:
        r = _sync_redis_client()
        # sets are not JSON-serialisable — convert to sorted list
        serialisable = {
            k: sorted(v) if isinstance(v, set) else v
            for k, v in stat_data.items()
        }
        r.hset(_KEY_GLOBALS, strategy, json.dumps(serialisable))
    except Exception as exc:
        logger.warning("execution_persistence.global_stat_write_failed", error=str(exc)[:80])


async def pop_opportunities(redis_url: str, max_count: int = 10) -> list[dict]:
    """Pop up to max_count opportunities from the pending queue (FIFO — RPOP).

    Called by OpportunitiesProcessorJob.
    Returns parsed opportunity dicts; malformed entries are discarded.
    """
    if not redis_url:
        return []
    results = []
    try:
        import redis.asyncio as _aioredis
        r = await _aioredis.from_url(redis_url)
        try:
            for _ in range(max_count):
                raw = await r.rpop(_KEY_OPPORTUNITIES)
                if raw is None:
                    break
                try:
                    results.append(json.loads(raw))
                except Exception:
                    pass  # Discard malformed entries
        finally:
            await r.aclose()
    except Exception as exc:
        logger.warning("execution_persistence.pop_opportunities_failed", error=str(exc)[:80])
    return results


async def get_opportunities_queue_depth(redis_url: str) -> int:
    """Return current depth of the opportunities:pending queue."""
    if not redis_url:
        return 0
    try:
        import redis.asyncio as _aioredis
        r = await _aioredis.from_url(redis_url)
        try:
            return await r.llen(_KEY_OPPORTUNITIES)
        finally:
            await r.aclose()
    except Exception:
        return 0


# ── Async load helpers (called once at startup) ────────────────────────────────

async def load_execution_knowledge(redis_url: str) -> dict:
    """Load all persisted execution knowledge at startup.

    Reads from PostgreSQL first (authoritative), then merges any Redis keys
    not yet synced to PG (handles crash between write and sync job).

    Returns a dict with four keys:
      strategy_scores  {"{domain}:{strategy}": dict}
      selectors        {"{domain}:{element_type}": str}
      global_stats     {strategy_name: dict}
      signal_weights   {signal_name: float}   ← EIM learned weights
    """
    import redis.asyncio as aioredis

    knowledge: dict = {
        "strategy_scores": {},
        "selectors": {},
        "global_stats": {},
        "signal_weights": {},
    }

    # ── PostgreSQL load ────────────────────────────────────────────────────────
    try:
        from ..db.session import async_session
        async with async_session() as session:
            from sqlalchemy import select, text
            result = await session.execute(
                text("SELECT key_type, domain, name, data FROM execution_knowledge")
            )
            rows = result.fetchall()

        for key_type, domain, name, data in rows:
            if key_type == "strategy_score":
                knowledge["strategy_scores"][f"{domain}:{name}"] = data
            elif key_type == "selector":
                knowledge["selectors"][f"{domain}:{name}"] = data.get("selector", "")
            elif key_type == "global_stat":
                knowledge["global_stats"][name] = data
            elif key_type == "signal_weights":
                # Stored as single row (domain="eim", name="weights")
                knowledge["signal_weights"] = {k: float(v) for k, v in data.items() if isinstance(v, (int, float, str))}
        logger.info(
            "execution_persistence.pg_loaded",
            scores=len(knowledge["strategy_scores"]),
            selectors=len(knowledge["selectors"]),
            globals=len(knowledge["global_stats"]),
        )
    except Exception as exc:
        logger.warning("execution_persistence.pg_load_failed", error=str(exc)[:120])

    # ── Redis merge (picks up writes not yet flushed to PG) ───────────────────
    try:
        r = aioredis.from_url(redis_url, decode_responses=True)

        raw_scores   = await r.hgetall(_KEY_SCORES)
        raw_selectors = await r.hgetall(_KEY_SELECTORS)
        raw_globals  = await r.hgetall(_KEY_GLOBALS)
        raw_weights  = await r.hgetall(_KEY_SIGNAL_WEIGHTS)

        await r.aclose()

        for field, val in raw_scores.items():
            if field not in knowledge["strategy_scores"]:
                try:
                    knowledge["strategy_scores"][field] = json.loads(val)
                except Exception:
                    pass

        for field, val in raw_selectors.items():
            if field not in knowledge["selectors"]:
                knowledge["selectors"][field] = val

        for field, val in raw_globals.items():
            if field not in knowledge["global_stats"]:
                try:
                    knowledge["global_stats"][field] = json.loads(val)
                except Exception:
                    pass

        # Signal weights: Redis is merged only if PG row is absent (PG is authoritative)
        if raw_weights and not knowledge["signal_weights"]:
            for k, v in raw_weights.items():
                try:
                    knowledge["signal_weights"][k] = float(v)
                except (ValueError, TypeError):
                    pass

        logger.info(
            "execution_persistence.redis_merged",
            scores_added=len(raw_scores) - len(knowledge["strategy_scores"]) + len(raw_scores),
        )
    except Exception as exc:
        logger.warning("execution_persistence.redis_load_failed", error=str(exc)[:120])

    return knowledge


def apply_loaded_knowledge(knowledge: dict) -> None:
    """Populate in-memory structures from the loaded knowledge dict.

    Called after load_execution_knowledge() in main.py.
    Directly patches the module-level dicts in reflection_engine and
    execution_planner — no restart, no re-import needed.
    """
    from .reflection_engine import (
        StrategyScore, _STRATEGY_SCORES, _GLOBAL_STRATEGY_STATS, _domain_clean
    )
    from .execution_planner import _SELECTOR_REGISTRY

    restored_scores = 0
    restored_selectors = 0
    restored_globals = 0

    # Strategy scores
    for composite_key, data in knowledge["strategy_scores"].items():
        if ":" not in composite_key:
            continue
        domain, _, strategy = composite_key.partition(":")
        if not strategy:
            continue
        score = StrategyScore(strategy=strategy, domain=domain)
        score.success_count      = data.get("success_count", 0)
        score.failure_count      = data.get("failure_count", 0)
        score.total_time_ms      = data.get("total_time_ms", 0)
        score.fallback_position_sum = data.get("fallback_position_sum", 0)
        score.executions         = data.get("executions", 0)
        _STRATEGY_SCORES[composite_key] = score
        restored_scores += 1

    # Selectors
    for composite_key, selector in knowledge["selectors"].items():
        if ":" not in composite_key or not selector:
            continue
        domain, _, element_type = composite_key.partition(":")
        if not element_type:
            continue
        if domain not in _SELECTOR_REGISTRY:
            _SELECTOR_REGISTRY[domain] = {}
        # Only restore if not overriding a manually curated seed entry
        if element_type not in _SELECTOR_REGISTRY[domain]:
            _SELECTOR_REGISTRY[domain][element_type] = selector
            restored_selectors += 1

    # Global stats
    for strategy, data in knowledge["global_stats"].items():
        if strategy not in _GLOBAL_STRATEGY_STATS:
            _GLOBAL_STRATEGY_STATS[strategy] = {
                "success": data.get("success", 0),
                "failure": data.get("failure", 0),
                "domains_won": set(data.get("domains_won", [])),
                "domains_tried": set(data.get("domains_tried", [])),
            }
        restored_globals += 1

    # Signal weights — write back to Redis so detection cycle uses restored values
    # (covers the case where Redis was flushed but PG had the weights)
    restored_weights = 0
    if knowledge.get("signal_weights"):
        try:
            r = _sync_redis_client()
            r.hset(
                _KEY_SIGNAL_WEIGHTS,
                mapping={k: str(v) for k, v in knowledge["signal_weights"].items()},
            )
            restored_weights = len(knowledge["signal_weights"])
        except Exception as exc:
            logger.warning("execution_persistence.signal_weights_restore_failed", error=str(exc)[:60])

    logger.info(
        "execution_persistence.knowledge_restored",
        scores=restored_scores,
        selectors=restored_selectors,
        global_stats=restored_globals,
        signal_weights=restored_weights,
    )


# ── PostgreSQL sync job helper (called by ExecutionKnowledgeSyncJob) ──────────

async def sync_to_postgres() -> dict:
    """Read all execution knowledge from Redis and upsert into PostgreSQL.

    Returns summary dict with counts for logging.
    """
    import redis.asyncio as aioredis
    from ..db.session import async_session
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from ..db.models import ExecutionKnowledge

    if not _redis_url:
        return {"error": "not_configured"}

    summary = {"scores": 0, "selectors": 0, "global_stats": 0, "signal_weights": 0, "errors": 0}

    try:
        r = aioredis.from_url(_redis_url, decode_responses=True)
        raw_scores    = await r.hgetall(_KEY_SCORES)
        raw_selectors  = await r.hgetall(_KEY_SELECTORS)
        raw_globals   = await r.hgetall(_KEY_GLOBALS)
        raw_weights   = await r.hgetall(_KEY_SIGNAL_WEIGHTS)
        await r.aclose()
    except Exception as exc:
        logger.warning("execution_persistence.sync_redis_read_failed", error=str(exc)[:80])
        return {"error": str(exc)[:80]}

    now = datetime.now(timezone.utc)

    async with async_session() as session:
        try:
            # Strategy scores
            for field, val in raw_scores.items():
                try:
                    data = json.loads(val)
                    domain, _, name = field.partition(":")
                    stmt = pg_insert(ExecutionKnowledge).values(
                        key_type="strategy_score", domain=domain, name=name,
                        data=data, updated_at=now,
                    ).on_conflict_do_update(
                        constraint="uq_execution_knowledge",
                        set_={"data": data, "updated_at": now},
                    )
                    await session.execute(stmt)
                    summary["scores"] += 1
                except Exception:
                    summary["errors"] += 1

            # Selectors
            for field, selector in raw_selectors.items():
                try:
                    domain, _, name = field.partition(":")
                    stmt = pg_insert(ExecutionKnowledge).values(
                        key_type="selector", domain=domain, name=name,
                        data={"selector": selector}, updated_at=now,
                    ).on_conflict_do_update(
                        constraint="uq_execution_knowledge",
                        set_={"data": {"selector": selector}, "updated_at": now},
                    )
                    await session.execute(stmt)
                    summary["selectors"] += 1
                except Exception:
                    summary["errors"] += 1

            # Global stats
            for strategy, val in raw_globals.items():
                try:
                    data = json.loads(val)
                    stmt = pg_insert(ExecutionKnowledge).values(
                        key_type="global_stat", domain="", name=strategy,
                        data=data, updated_at=now,
                    ).on_conflict_do_update(
                        constraint="uq_execution_knowledge",
                        set_={"data": data, "updated_at": now},
                    )
                    await session.execute(stmt)
                    summary["global_stats"] += 1
                except Exception:
                    summary["errors"] += 1

            # Signal weights (EIM learned weights — single row upsert)
            if raw_weights:
                try:
                    weights_data = {}
                    for k, v in raw_weights.items():
                        try:
                            weights_data[k] = float(v)
                        except (ValueError, TypeError):
                            pass
                    if weights_data:
                        stmt = pg_insert(ExecutionKnowledge).values(
                            key_type="signal_weights", domain="eim", name="weights",
                            data=weights_data, updated_at=now,
                        ).on_conflict_do_update(
                            constraint="uq_execution_knowledge",
                            set_={"data": weights_data, "updated_at": now},
                        )
                        await session.execute(stmt)
                        summary["signal_weights"] = 1
                except Exception:
                    summary["errors"] += 1

            await session.commit()
        except Exception as exc:
            logger.warning("execution_persistence.sync_pg_write_failed", error=str(exc)[:120])
            summary["errors"] += 1
            await session.rollback()

    logger.info("execution_persistence.sync_complete", **summary)
    return summary
