"""Agent WASP 24/7 Autonomous Continuous Worker Daemon.

Runs continuously in the background on the Windows host:
1. ATSE Self-Evolution Loop: Runs every 30 minutes (AST entropy, Curiosity, Common-Sense Gate, Triad Consensus, Shadow Staging).
2. Workforce Shift Loop: Runs every 60 minutes (Prometheus health check, Hermes inbox triage, Athena repo audit, briefing email).
3. Real-time Heartbeat: Updates status every 60 seconds.
"""

import os
import sys
import time
import asyncio
from pathlib import Path
from datetime import datetime, timezone

# Add agent-core to path
ROOT_DIR = Path(__file__).resolve().parent.parent
AGENT_CORE_DIR = str(ROOT_DIR / "containers" / "agent-core")
sys.path.insert(0, AGENT_CORE_DIR)
sys.stdout.reconfigure(encoding="utf-8")

import structlog
import fakeredis.aioredis
import redis.asyncio as aioredis

# Shared in-memory Redis fallback for native Windows execution
fake_r = fakeredis.aioredis.FakeRedis(decode_responses=True)
aioredis.from_url = lambda url, **kwargs: fake_r

logger = structlog.get_logger()

ATSE_INTERVAL_SECONDS = int(os.getenv("WASP_ATSE_INTERVAL", 1800))       # 30 minutes
SHIFT_INTERVAL_SECONDS = int(os.getenv("WASP_SHIFT_INTERVAL", 3600))     # 60 minutes
HEARTBEAT_INTERVAL_SECONDS = 60                                          # 1 minute


async def run_atse_cycle():
    """Execute one Autonomous Teleological Self-Evolution cycle."""
    try:
        from src.scheduler.autonomic_evolution_job import AutonomicEvolutionJob
        job = AutonomicEvolutionJob(workspace_root=ROOT_DIR, redis_client=fake_r, dry_run=True)
        logger.info("daemon.atse_cycle_starting", timestamp=datetime.now(timezone.utc).isoformat())
        result = await job.execute_cycle()
        logger.info("daemon.atse_cycle_completed", result=result)
        return result
    except Exception as exc:
        logger.error("daemon.atse_cycle_failed", error=str(exc)[:120])
        return {"ok": False, "error": str(exc)}


async def run_workforce_shift():
    """Execute one multi-agent employee shift (Prometheus, Hermes, Athena)."""
    try:
        logger.info("daemon.workforce_shift_starting", timestamp=datetime.now(timezone.utc).isoformat())
        script_path = os.path.join(os.path.dirname(__file__), "run_active_workforce_shift.py")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, script_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            logger.info("daemon.workforce_shift_completed")
        else:
            logger.warning("daemon.workforce_shift_warning", code=proc.returncode, stderr=stderr.decode()[:120])
    except Exception as exc:
        logger.error("daemon.workforce_shift_failed", error=str(exc)[:120])


async def main_loop():
    print("=" * 70)
    print("🐝 AGENT WASP 24/7 AUTONOMOUS CONTINUOUS WORKER DAEMON STARTED")
    print(f"ATSE Evolution Interval: {ATSE_INTERVAL_SECONDS}s ({ATSE_INTERVAL_SECONDS // 60}m)")
    print(f"Workforce Shift Interval: {SHIFT_INTERVAL_SECONDS}s ({SHIFT_INTERVAL_SECONDS // 60}m)")
    print(f"Host: Windows | PID: {os.getpid()}")
    print("=" * 70)

    last_atse = 0.0
    last_shift = 0.0

    # Initial runs on boot
    await run_atse_cycle()
    last_atse = time.time()

    while True:
        now = time.time()

        # Check ATSE Cycle
        if now - last_atse >= ATSE_INTERVAL_SECONDS:
            await run_atse_cycle()
            last_atse = time.time()

        # Check Workforce Shift Cycle
        if now - last_shift >= SHIFT_INTERVAL_SECONDS:
            await run_workforce_shift()
            last_shift = time.time()

        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        print("\n[WASP] Daemon stopped by operator.")
