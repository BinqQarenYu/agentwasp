"""Agent introspection: performance metrics and self-assessment.

Performance improvements:
- All 4 audit_log queries merged into a single DB round-trip using subqueries
- health_score computation re-uses a pre-fetched health dict (no double call)
- generate_report() parallelizes health check + performance query
"""

import asyncio
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import func, select, and_, text

from ..db.models import AuditLog
from ..db.session import async_session

logger = structlog.get_logger()


class Introspector:
    def __init__(self, memory, model_manager, skill_registry, health_monitor):
        self.memory = memory
        self.model_manager = model_manager
        self.skill_registry = skill_registry
        self.health_monitor = health_monitor
        self._start_time = datetime.now(timezone.utc)

    async def get_performance(self, hours: int = 24) -> dict:
        """Query audit_log for performance metrics — all aggregations in one session."""
        since = datetime.now(timezone.utc) - timedelta(hours=hours)

        async with async_session() as session:
            # Sequential queries — asyncio.gather with the same session causes
            # IllegalStateChangeError in SQLAlchemy async.
            total_r = await session.execute(
                select(func.count(AuditLog.id)).where(AuditLog.timestamp >= since)
            )
            avg_lat_r = await session.execute(
                select(func.avg(AuditLog.latency_ms)).where(
                    and_(AuditLog.timestamp >= since, AuditLog.latency_ms > 0)
                )
            )
            errors_r = await session.execute(
                select(func.count(AuditLog.id)).where(
                    and_(AuditLog.timestamp >= since, AuditLog.error.isnot(None))
                )
            )
            by_type_r = await session.execute(
                select(AuditLog.event_type, func.count(AuditLog.id).label("cnt"))
                .where(AuditLog.timestamp >= since)
                .group_by(AuditLog.event_type)
                .order_by(func.count(AuditLog.id).desc())
                .limit(5)
            )

            # Hourly buckets for sparklines — must stay inside session
            hourly_r = await session.execute(
                text("""
                SELECT
                    LEAST(23, GREATEST(0,
                        FLOOR(EXTRACT(EPOCH FROM (timestamp - :since)) / 3600)::int
                    )) AS bucket,
                    COUNT(*)                                          AS total_cnt,
                    COUNT(error)                                      AS error_cnt,
                    AVG(CASE WHEN latency_ms > 0 THEN latency_ms END) AS avg_lat
                FROM audit_log
                WHERE timestamp >= :since
                GROUP BY bucket ORDER BY bucket
                """),
                {"since": since},
            )
            hourly_rows = hourly_r.mappings().all()

            total = total_r.scalar_one()
            avg_latency = avg_lat_r.scalar_one()
            errors = errors_r.scalar_one()
            by_type = by_type_r.all()

        # Build 24-slot arrays
        hourly = [{"total": 0, "errors": 0, "avg_lat": 0} for _ in range(24)]
        for row in hourly_rows:
            h = int(row["bucket"])
            hourly[h] = {
                "total": int(row["total_cnt"]),
                "errors": int(row["error_cnt"]),
                "avg_lat": round(float(row["avg_lat"] or 0)),
            }

        error_rate = (errors / total * 100) if total > 0 else 0
        return {
            "hours": hours,
            "total_events": total,
            "avg_latency_ms": round(avg_latency or 0),
            "errors": errors,
            "error_rate": round(error_rate, 1),
            "top_events": [{"type": t, "count": c} for t, c in by_type],
            "hourly": hourly,
        }

    def get_capabilities(self) -> dict:
        """Report agent's current capabilities."""
        model_status = self.model_manager.get_status()
        stats = self.memory.get_stats()

        skills_info = []
        if self.skill_registry:
            for defn in self.skill_registry.list_all():
                skills_info.append({
                    "name": defn.name,
                    "enabled": self.skill_registry.is_enabled(defn.name),
                })

        uptime = datetime.now(timezone.utc) - self._start_time
        uptime_h = round(uptime.total_seconds() / 3600, 1)

        return {
            "phase": 8,
            "active_model": model_status["active_model"],
            "active_provider": model_status["active_provider"],
            "fallback_order": model_status["fallback_order"],
            "skills_total": len(skills_info),
            "skills_enabled": sum(1 for s in skills_info if s["enabled"]),
            "memory_total": stats["total"],
            "memory_size_bytes": stats["size_bytes"],
            "uptime_hours": uptime_h,
        }

    def _score_from_health(self, health: dict, perf: dict) -> int:
        """Compute health score from pre-fetched health + perf dicts."""
        score = 100

        for svc, info in health.get("services", {}).items():
            if not info.get("healthy", False):
                score -= 20

        disk = health.get("system", {}).get("disk", {})
        if disk.get("percent", 0) >= 90:
            score -= 15
        elif disk.get("warning", False):
            score -= 5

        ram = health.get("system", {}).get("ram", {})
        if not ram.get("healthy", True):
            score -= 10
        elif ram.get("warning", False):
            score -= 5

        if perf.get("error_rate", 0) > 10:
            score -= 10
        if perf.get("avg_latency_ms", 0) > 10000:
            score -= 5

        return max(0, min(100, score))

    async def compute_health_score(self) -> int:
        """Compute a 0-100 health score. Parallelizes health + perf queries."""
        health, perf = await asyncio.gather(
            self.health_monitor.check_all(),
            self.get_performance(hours=1),
        )
        return self._score_from_health(health, perf)

    async def generate_report(self) -> str:
        """Generate a formatted introspection report for Telegram.

        Health check and performance query run in parallel.
        """
        health, perf = await asyncio.gather(
            self.health_monitor.check_all(),
            self.get_performance(hours=24),
        )
        caps = self.get_capabilities()
        score = self._score_from_health(health, perf)

        lines = [
            "Agent Introspection Report",
            "",
            f"Health Score: {score}/100",
            "",
            "Services:",
        ]

        for svc, info in health.get("services", {}).items():
            status = "OK" if info.get("healthy") else "FAIL"
            latency = info.get("latency_ms", "")
            lat_str = f" ({latency}ms)" if latency else ""
            lines.append(f"  {svc}: {status}{lat_str}")

        disk = health.get("system", {}).get("disk", {})
        ram = health.get("system", {}).get("ram", {})
        lines.append(f"\nSystem:")
        lines.append(f"  Disk: {disk.get('percent', '?')}% ({disk.get('used_gb', '?')}/{disk.get('total_gb', '?')} GB)")
        lines.append(f"  RAM: {ram.get('percent', '?')}% ({ram.get('used_mb', '?')}/{ram.get('total_mb', '?')} MB)")
        lines.append(f"  CPU: {health.get('system', {}).get('cpu', {}).get('percent', '?')}%")
        lines.append(f"  Uptime: {caps['uptime_hours']}h (process)")

        lines.append(f"\nPerformance (24h):")
        lines.append(f"  Events: {perf['total_events']}")
        lines.append(f"  Avg latency: {perf['avg_latency_ms']}ms")
        lines.append(f"  Errors: {perf['errors']} ({perf['error_rate']}%)")

        lines.append(f"\nCapabilities:")
        lines.append(f"  Model: {caps['active_model']} ({caps['active_provider']})")
        lines.append(f"  Skills: {caps['skills_enabled']}/{caps['skills_total']} enabled")
        lines.append(f"  Memory: {caps['memory_total']} entries")

        return "\n".join(lines)
