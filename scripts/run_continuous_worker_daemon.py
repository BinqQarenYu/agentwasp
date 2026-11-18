"""Agent WASP 24/7 Autonomous Continuous Worker Daemon.

Runs continuously in the background on the Windows host:
1. ATSE Self-Evolution Loop: Runs every 30 minutes (AST entropy, Curiosity, Common-Sense Gate, Triad Consensus, Shadow Staging).
2. Workforce Shift Loop: Runs every 60 minutes (Prometheus health check, Hermes inbox triage, Athena repo audit, briefing email).
3. Real-time Heartbeat: Updates status every 60 seconds.
"""

import os
import sys
import time
import socket
import asyncio
from pathlib import Path
from datetime import datetime, timezone

# 1. Enforce Single Instance Socket Lock on Host Port 14348
_daemon_lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    _daemon_lock.bind(("127.0.0.1", 14348))
    _daemon_lock.listen(1)
except socket.error:
    print("[Agent WASP] Continuous worker daemon is already running (port 14348 locked). Exiting duplicate.")
    sys.exit(0)

# 2. Enforce Host CPU Safety: Set Process Priority to BelowNormal
try:
    import psutil
    psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
except Exception:
    pass

# Add agent-core to path
ROOT_DIR = Path(__file__).resolve().parent.parent
AGENT_CORE_DIR = str(ROOT_DIR / "containers" / "agent-core")
sys.path.insert(0, AGENT_CORE_DIR)

# Handle pythonw execution (where sys.stdout and sys.stderr are None)
if sys.stdout is None:
    log_dir = ROOT_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    _log_f = open(log_dir / "worker_daemon.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = _log_f
    sys.stderr = _log_f
else:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

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


_atse_job = None


async def run_atse_cycle():
    """Execute one Autonomous Teleological Self-Evolution cycle with persistent target rotation."""
    global _atse_job
    try:
        from src.scheduler.autonomic_evolution_job import AutonomicEvolutionJob
        if _atse_job is None:
            _atse_job = AutonomicEvolutionJob(workspace_root=ROOT_DIR, redis_client=fake_r, dry_run=True)
        logger.info("daemon.atse_cycle_starting", timestamp=datetime.now(timezone.utc).isoformat())
        result = await _atse_job.execute_cycle()
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
