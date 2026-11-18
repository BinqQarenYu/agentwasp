"""MueHomeostasisJob — Native in-container 24/7 Scheduler Job for Bot 16 (Mue).

Runs natively inside agent-core where Redis (agent-redis:6379) and Postgres
are directly reachable, ensuring:
1. Every homeostasis shift persists live diagnostic hypotheses to Cognitive Blackboard (/blackboard).
2. Live Effective Homeostatic Loop (C_eff = C_code * Omega_sense) scans real codebase AST entropy
   and rotates across highest-entropy modules.
3. Dynamic AI provider cascade resolves Google Free Tier -> Ollama Local -> Deterministic safety net.
4. Updates Redis key `mue:last_shift` and synchronizes `goal-mue-homeostasis-startup` telemetry.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import redis.asyncio as aioredis
import structlog

from ..health.mue_care_engine import MueCareEngine

logger = structlog.get_logger()

MUE_LAST_SHIFT_KEY = "mue:last_shift"
MUE_HISTORY_KEY = "mue:shift_history"
MUE_HISTORY_MAX = 48


class MueHomeostasisJob:
    """Periodic self-care, AST curiosity, and homeostasis physician job for Bot 16 (Mue)."""

    def __init__(
        self,
        redis_url: str,
        bus=None,
        workspace_root: Optional[Path] = None,
        ollama_host: str = "",
    ) -> None:
        self.redis_url = redis_url
        self.bus = bus
        self.ollama_host = ollama_host or os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
        self.engine = MueCareEngine(
            workspace_root=workspace_root,
            redis_url=self.redis_url,
            ollama_host=self.ollama_host,
        )

    async def _persist_shift_telemetry(self, shift: dict[str, Any]) -> None:
        """Persist latest shift snapshot and update Mue's startup goal telemetry in Redis."""
        if not self.redis_url:
            return
        r = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            payload = dict(shift)
            payload["timestamp"] = datetime.now(timezone.utc).isoformat()
            raw = json.dumps(payload, default=str)
            await r.set(MUE_LAST_SHIFT_KEY, raw)
            await r.lpush(MUE_HISTORY_KEY, raw)
            await r.ltrim(MUE_HISTORY_KEY, 0, MUE_HISTORY_MAX - 1)

            # Refresh startup goal task outputs if goal exists
            try:
                from ..goal_orchestrator.store import load_goal, save_goal

                goal = await load_goal(r, "goal-mue-homeostasis-startup")
                if goal and goal.task_graph and goal.task_graph.nodes:
                    eff = shift.get("effective_loop", {})
                    c_m = eff.get("curiosity_metrics", {})
                    cs_m = eff.get("common_sense_metrics", {})
                    vitals = shift.get("vitals", {})
                    hygiene = shift.get("hygiene", {})
                    pad = shift.get("pad", {})

                    updates = {
                        "task-mue-vitals": (
                            f"RAM: {vitals.get('ram_percent')}%, CPU: {vitals.get('cpu_percent')}%, "
                            f"AI: {shift.get('ai_provider')} ({shift.get('ai_tier')})"
                        ),
                        "task-mue-hygiene": (
                            f"Pruned: {hygiene.get('shadow_sessions_pruned')} sessions, "
                            f"Rotated: {hygiene.get('backup_files_rotated')} backups, "
                            f"Reclaimed: {hygiene.get('bytes_reclaimed')} bytes"
                        ),
                        "task-mue-pad": (
                            f"Vitality: {shift.get('vitality_index')}, State: {shift.get('state')}, "
                            f"Pleasure: {pad.get('pleasure')}, Arousal: {pad.get('arousal')}"
                        ),
                        "task-mue-curiosity-research": (
                            f"C_code: {eff.get('c_code')} on {eff.get('target_file')} "
                            f"(Entropy: {c_m.get('entropy')}, Surprise: {c_m.get('surprise')})"
                        ),
                        "task-mue-commonsense-research": (
                            f"Omega_sense: {eff.get('omega_sense')} "
                            f"(Drift: {cs_m.get('teleological_drift')}, Passes: {cs_m.get('passes_gate')})"
                        ),
                        "task-mue-effective-loop": (
                            f"C_eff: {eff.get('c_effective')} -> {eff.get('loop_status')} "
                            f"(Target: {eff.get('target_file')})"
                        ),
                        "task-mue-blackboard": f"Hypothesis Published: {shift.get('hypothesis_id') or 'none'}",
                    }
                    for node in goal.task_graph.nodes:
                        if node.id in updates:
                            node.output_summary = updates[node.id]
                    await save_goal(r, goal)
            except Exception as goal_err:
                logger.debug("mue.goal_sync_skipped", error=str(goal_err)[:100])
        finally:
            await r.aclose()

    async def __call__(self) -> str:
        """Invoked by agent-core Scheduler."""
        try:
            shift = await self.engine.perform_full_shift()
            await self._persist_shift_telemetry(shift)
            eff = shift.get("effective_loop", {})
            return (
                f"Mue Shift: state={shift.get('state')} vitality={shift.get('vitality_index')} "
                f"C_eff={eff.get('c_effective')} ({eff.get('loop_status')}) "
                f"target={eff.get('target_file')} ai={shift.get('ai_provider')} "
                f"hyp={shift.get('hypothesis_id')}"
            )
        except Exception as exc:
            logger.exception("mue_homeostasis.job_failed", error=str(exc))
            return f"Mue Shift error: {str(exc)[:100]}"
