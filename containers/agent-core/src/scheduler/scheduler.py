"""Lightweight asyncio-based scheduler for periodic background jobs.

Uses asyncio.create_task with sleep loops.
Job pause state is persisted to Redis so it survives container restarts.
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Awaitable, Optional
from uuid import uuid4

import structlog

from ..db.models import AuditLog
from ..db.session import async_session

logger = structlog.get_logger()

# Redis key for persisted job state
_STATE_KEY = "scheduler:job_state"

# Jobs that should NOT send failure Telegram notifications (internal/infra jobs)
_SILENT_JOBS = frozenset({
    "health_check", "reflection", "memory_cleanup", "memory_pruner",
    "db_maintenance", "audit_retention", "vector_index", "kg_insights",
    "execution_knowledge_sync", "cpi_monitor", "self_integrity",
    "skill_evolution", "capability_evolution", "capability_learner",
    "behavioral_learner", "procedural_pruner", "execution_reflection_pruner",
    "opportunities_processor", "world_model",
})


@dataclass
class JobState:
    name: str
    interval_seconds: float
    callback: Callable[[], Awaitable[str]]
    paused: bool = False
    last_run: str = ""
    last_result: str = ""
    last_success: bool = True
    run_count: int = 0
    failure_count: int = 0
    _task: asyncio.Task | None = field(default=None, repr=False)


class Scheduler:
    """Manages periodic background jobs as asyncio tasks.

    Job pause/resume state is persisted in Redis (key: scheduler:job_state)
    so manual pauses survive container restarts.
    """

    def __init__(
        self,
        redis_url: str | None = None,
        bus=None,
        notify_chat_id: str = "",
    ):
        self._jobs: dict[str, JobState] = {}
        self._shutdown = asyncio.Event()
        self._redis_url = redis_url
        self._bus = bus                        # EventBus for failure notifications
        self._notify_chat_id = notify_chat_id  # Telegram chat to notify on job failure

    def register(
        self,
        name: str,
        interval_seconds: float,
        callback: Callable[[], Awaitable[str]],
    ) -> None:
        try:
            if not name or not callable(callback):
                raise ValueError(f"invalid job spec: name={name!r} callback={callback!r}")
            if interval_seconds <= 0:
                raise ValueError(f"invalid interval for {name}: {interval_seconds}")
            self._jobs[name] = JobState(
                name=name,
                interval_seconds=interval_seconds,
                callback=callback,
            )
            logger.info("scheduler.registered", job=name, interval=interval_seconds)
        except Exception:
            # Surface registration failures loudly — silent skips have caused
            # jobs to vanish from scheduler:job_state without any indication.
            logger.exception("scheduler.register_failed", job=name, interval=interval_seconds)
            raise

    async def start(self) -> None:
        """Start all registered jobs as background asyncio tasks."""
        # Restore persisted pause state before launching
        await self._restore_state()
        for name, job in self._jobs.items():
            job._task = asyncio.create_task(self._run_loop(job))
        logger.info("scheduler.started", jobs=list(self._jobs.keys()))

    async def stop(self) -> None:
        """Cancel all running job tasks."""
        self._shutdown.set()
        for name, job in self._jobs.items():
            if job._task and not job._task.done():
                job._task.cancel()
                try:
                    await job._task
                except asyncio.CancelledError:
                    pass
        logger.info("scheduler.stopped")

    async def trigger(self, job_name: str) -> str:
        """Manually trigger a job immediately."""
        job = self._jobs.get(job_name)
        if not job:
            return f"Job '{job_name}' not found."
        return await self._execute_job(job)

    async def pause(self, job_name: str) -> bool:
        job = self._jobs.get(job_name)
        if not job:
            return False
        job.paused = True
        await self._persist_state()
        logger.info("scheduler.job_paused", job=job_name)
        return True

    async def resume(self, job_name: str) -> bool:
        job = self._jobs.get(job_name)
        if not job:
            return False
        job.paused = False
        await self._persist_state()
        logger.info("scheduler.job_resumed", job=job_name)
        return True

    # Keep sync aliases for dashboard code that calls without await
    def pause_sync(self, job_name: str) -> bool:
        job = self._jobs.get(job_name)
        if job:
            job.paused = True
        return bool(job)

    def resume_sync(self, job_name: str) -> bool:
        job = self._jobs.get(job_name)
        if job:
            job.paused = False
        return bool(job)

    async def _persist_state(self) -> None:
        """Persist job pause state and last_run to Redis."""
        if not self._redis_url:
            return
        try:
            import redis.asyncio as aioredis
            r = aioredis.from_url(self._redis_url, decode_responses=True)
            state = {
                name: {
                    "paused": job.paused,
                    "run_count": job.run_count,
                    "last_run": job.last_run,
                    "failure_count": job.failure_count,
                }
                for name, job in self._jobs.items()
            }
            await r.set(_STATE_KEY, json.dumps(state), ex=86400 * 30)
            await r.aclose()
        except Exception:
            logger.warning("scheduler.persist_state_failed")

    async def _restore_state(self) -> None:
        """Restore job pause state and last_run from Redis."""
        if not self._redis_url:
            return
        try:
            import redis.asyncio as aioredis
            r = aioredis.from_url(self._redis_url, decode_responses=True)
            raw = await r.get(_STATE_KEY)
            await r.aclose()
            if not raw:
                return
            state = json.loads(raw)
            restored = []
            for name, saved in state.items():
                job = self._jobs.get(name)
                if not job:
                    continue
                if saved.get("paused"):
                    job.paused = True
                    restored.append(name)
                if saved.get("last_run"):
                    job.last_run = saved["last_run"]
                if saved.get("run_count"):
                    job.run_count = saved["run_count"]
                if saved.get("failure_count"):
                    job.failure_count = saved["failure_count"]
            if restored:
                logger.info("scheduler.state_restored", paused_jobs=restored)
        except Exception:
            logger.warning("scheduler.restore_state_failed")

    def list_jobs(self) -> list[dict]:
        """Return status of all jobs."""
        now = datetime.now(timezone.utc)
        result = []
        for name, job in self._jobs.items():
            # Calculate next_run from last_run + interval
            next_run = ""
            if job.last_run:
                try:
                    last_dt = datetime.fromisoformat(job.last_run)
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    next_dt = last_dt + timedelta(seconds=job.interval_seconds)
                    # Human-friendly: "in Xm Ys" or "overdue"
                    delta = (next_dt - now).total_seconds()
                    if delta < 0:
                        next_run = "overdue"
                    elif delta < 60:
                        next_run = f"in {int(delta)}s"
                    elif delta < 3600:
                        next_run = f"in {int(delta // 60)}m {int(delta % 60)}s"
                    else:
                        next_run = f"in {int(delta // 3600)}h {int((delta % 3600) // 60)}m"
                except Exception:
                    pass

            result.append({
                "name": name,
                "interval_seconds": job.interval_seconds,
                "paused": job.paused,
                "last_run": job.last_run,
                "last_result": (job.last_result or "")[:800],
                "last_success": job.last_success,
                "run_count": job.run_count,
                "failure_count": job.failure_count,
                "next_run": next_run,
            })
        return result

    async def _run_loop(self, job: JobState) -> None:
        """Periodic execution loop for a single job.

        After startup, if a job's persisted ``last_run`` is older than its
        interval, execute immediately rather than waiting the full interval
        again — otherwise long-interval jobs (daily/weekly) never run on
        hosts that restart more often than the interval.
        """
        initial_delay = 5.0 if job.name == "reminder_checker" else 30.0 if job.name == "health_check" else 60.0
        await asyncio.sleep(initial_delay)

        # Catch-up: if the job is overdue based on persisted last_run, the
        # first iteration runs immediately. Otherwise, sleep the remaining
        # time so we don't double-execute right after a fast restart.
        first_sleep_override: float | None = None
        if job.last_run:
            try:
                last_dt = datetime.fromisoformat(job.last_run)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
                if elapsed >= job.interval_seconds:
                    first_sleep_override = 0.0
                    logger.info(
                        "scheduler.catchup",
                        job=job.name,
                        elapsed_s=int(elapsed),
                        interval_s=job.interval_seconds,
                    )
                else:
                    first_sleep_override = max(0.0, job.interval_seconds - elapsed)
            except Exception:
                first_sleep_override = None

        # If we have a remaining-interval override and it's > 0, sleep that
        # before the first execution rather than running immediately at boot
        # for non-overdue jobs.
        if first_sleep_override is not None and first_sleep_override > 0:
            remaining = first_sleep_override
            while remaining > 0 and not self._shutdown.is_set():
                sleep_time = min(remaining, 5.0)
                await asyncio.sleep(sleep_time)
                remaining -= sleep_time

        while not self._shutdown.is_set():
            if not job.paused:
                try:
                    await self._execute_job(job)
                except Exception:
                    logger.exception("scheduler.job_error", job=job.name)

            remaining = job.interval_seconds
            while remaining > 0 and not self._shutdown.is_set():
                sleep_time = min(remaining, 5.0)
                await asyncio.sleep(sleep_time)
                remaining -= sleep_time

    async def _execute_job(self, job: JobState) -> str:
        """Execute a single job, update state, write audit log."""
        start = time.monotonic()
        logger.info("scheduler.job_executing", job=job.name)

        try:
            result = await job.callback()
            job.last_success = True
            job.last_result = result
        except Exception as e:
            result = f"Error: {e}"
            job.last_success = False
            job.last_result = result
            job.failure_count += 1
            logger.exception("scheduler.job_failed", job=job.name)
            # Notify user via Telegram for visible jobs on first failure or every 5th
            if (
                self._bus
                and self._notify_chat_id
                and job.name not in _SILENT_JOBS
                and (job.failure_count == 1 or job.failure_count % 5 == 0)
            ):
                _reason = str(e)[:120]
                _msg = f"Tarea fallida: {job.name}\nRazón: {_reason}"
                try:
                    await self._bus.publish("events:outgoing", {
                        "event_type": "telegram.response",
                        "correlation_id": str(uuid4()),
                        "chat_id": self._notify_chat_id,
                        "text": _msg,
                    })
                except Exception:
                    pass

        job.last_run = datetime.now(timezone.utc).isoformat()
        job.run_count += 1
        latency_ms = int((time.monotonic() - start) * 1000)

        # Persist state after each execution so last_run survives restarts
        if self._redis_url:
            try:
                import redis.asyncio as aioredis
                r = aioredis.from_url(self._redis_url, decode_responses=True)
                existing_raw = await r.get(_STATE_KEY)
                state = json.loads(existing_raw) if existing_raw else {}
                state[job.name] = {
                    "paused": job.paused,
                    "run_count": job.run_count,
                    "last_run": job.last_run,
                    "failure_count": job.failure_count,
                }
                await r.set(_STATE_KEY, json.dumps(state), ex=86400 * 30)
                await r.aclose()
            except Exception:
                pass

        _should_audit = (
            not job.last_success
            or (
                result
                and isinstance(result, str)
                and len(result) > 40
                and result.strip().lower() not in {f"{job.name}: ok", "ok"}
            )
        )

        if _should_audit:
            try:
                async with async_session() as session:
                    audit = AuditLog(
                        id=str(uuid4()),
                        event_type=f"scheduled.{job.name}",
                        source="scheduler",
                        action=f"scheduler.{job.name}",
                        input_summary=f"interval={job.interval_seconds}s",
                        output_summary=(result or "")[:200],
                        latency_ms=latency_ms,
                        error=result if not job.last_success else None,
                    )
                    session.add(audit)
                    await session.commit()
            except Exception:
                logger.exception("scheduler.audit_error", job=job.name)

        logger.info(
            "scheduler.job_complete",
            job=job.name,
            success=job.last_success,
            ms=latency_ms,
        )
        return result
