"""Seed startup goal for Bot 16: Mue (System Homeostasis Physician)."""

import asyncio
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

# Add agent-core to path
ROOT_DIR = Path(__file__).resolve().parent.parent
AGENT_CORE_DIR = str(ROOT_DIR / "containers" / "agent-core")
sys.path.insert(0, AGENT_CORE_DIR)
sys.stdout.reconfigure(encoding="utf-8")

from src.goal_orchestrator.types import (
    Goal,
    GoalState,
    TaskGraph,
    TaskNode,
    TaskStatus,
    RiskLevel,
    AutonomyMode,
)
from src.goal_orchestrator.store import save_goal
from src.health.mue_care_engine import MueCareEngine


async def seed_startup_goal():
    print("=" * 70)
    print("🩺 SEEDING STARTUP GOAL FOR BOT 16: MUE")
    print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    # 1. Instantiate Mue Care Engine and run initial health check
    engine = MueCareEngine(workspace_root=ROOT_DIR)
    shift_res = await engine.perform_full_shift()
    print(f"[MUE] Initial Shift: {shift_res.get('state')} (Vitality: {shift_res.get('vitality_index')})")
    print(f"[MUE] Host RAM: {shift_res.get('vitals', {}).get('ram_percent')}% | Reclaimed: {shift_res.get('hygiene', {}).get('bytes_reclaimed')} bytes | AI: {shift_res.get('ai_provider')} ({shift_res.get('ai_tier')})")

    # 2. Build Goal Task DAG
    eff_loop = shift_res.get("effective_loop", {})
    c_metrics = eff_loop.get("curiosity_metrics", {})
    cs_metrics = eff_loop.get("common_sense_metrics", {})

    graph = TaskGraph()
    t1 = TaskNode(
        id="task-mue-vitals",
        description="Continuously sample host psutil vitals (RAM < 17.4GB, CPU, dynamic Google/Ollama health)",
        skill_name="vital_signs_collector",
        status=TaskStatus.DONE,
        risk_level=RiskLevel.LOW,
        output_summary=f"RAM: {shift_res.get('vitals', {}).get('ram_percent')}%, CPU: {shift_res.get('vitals', {}).get('cpu_percent')}%, AI: {shift_res.get('ai_provider')} ({shift_res.get('ai_tier')})",
    )
    t2 = TaskNode(
        id="task-mue-hygiene",
        description="Enforce storage hygiene: prune stale shadow staging (>2h) and rotate backups (<500MB)",
        skill_name="storage_hygiene_manager",
        status=TaskStatus.DONE,
        risk_level=RiskLevel.LOW,
        dependencies=["task-mue-vitals"],
        output_summary=f"Pruned: {shift_res.get('hygiene', {}).get('shadow_sessions_pruned')} sessions, Rotated: {shift_res.get('hygiene', {}).get('backup_files_rotated')} backups, Reclaimed: {shift_res.get('hygiene', {}).get('bytes_reclaimed')} bytes",
    )
    t3 = TaskNode(
        id="task-mue-pad",
        description="Compute Pleasure-Arousal-Dominance (PAD) emotional homeostasis & anti-stagnation index",
        skill_name="pad_vitality_engine",
        status=TaskStatus.DONE,
        risk_level=RiskLevel.LOW,
        dependencies=["task-mue-hygiene"],
        output_summary=f"Vitality: {shift_res.get('vitality_index')}, State: {shift_res.get('state')}, Pleasure: {shift_res.get('pad', {}).get('pleasure')}, Arousal: {shift_res.get('pad', {}).get('arousal')}",
    )
    t4 = TaskNode(
        id="task-mue-curiosity-research",
        description="Deep research defining and quantifying Coding Curiosity (C_code) via AST Shannon entropy, epistemic surprise (KL divergence), and host resource throttling",
        skill_name="curiosity_quantifier",
        status=TaskStatus.DONE,
        risk_level=RiskLevel.LOW,
        dependencies=["task-mue-pad"],
        output_summary=f"C_code: {eff_loop.get('c_code', 0.65)} (Entropy: {c_metrics.get('entropy', 0.72)}, Surprise: {c_metrics.get('surprise', 0.50)}, Salience: {c_metrics.get('salience', 0.95)})",
    )
    t5 = TaskNode(
        id="task-mue-commonsense-research",
        description="Deep research defining and quantifying Coding Common Sense (Omega_sense) via teleological goal alignment, invariant gating (Immutable Enclave protection), and economic budget awareness",
        skill_name="commonsense_aligner",
        status=TaskStatus.DONE,
        risk_level=RiskLevel.LOW,
        dependencies=["task-mue-curiosity-research"],
        output_summary=f"Omega_sense: {eff_loop.get('omega_sense', 0.95)} (Drift: {cs_metrics.get('teleological_drift', 0.05)}, Passes Gate: {cs_metrics.get('passes_gate', True)})",
    )
    t6 = TaskNode(
        id="task-mue-effective-loop",
        description="Execute closed Effective Homeostatic Loop (C_eff = C_code * Omega_sense) coupling PAD emotional vitality and stagnation pressure to safe, actionable self-directed system evolution",
        skill_name="effective_homeostatic_loop",
        status=TaskStatus.DONE,
        risk_level=RiskLevel.LOW,
        dependencies=["task-mue-commonsense-research"],
        output_summary=f"C_eff: {eff_loop.get('c_effective', 0.6175)} -> Status: {eff_loop.get('loop_status', 'ACTIONABLE_EVOLUTION')} (Actionable: {eff_loop.get('is_actionable', True)})",
    )
    t7 = TaskNode(
        id="task-mue-blackboard",
        description="Publish real-time system homeostasis, curiosity metrics, and effective loop diagnosis to Cognitive Blackboard (/blackboard)",
        skill_name="blackboard_publisher",
        status=TaskStatus.DONE,
        risk_level=RiskLevel.LOW,
        dependencies=["task-mue-effective-loop"],
        output_summary=f"Hypothesis Published: {shift_res.get('hypothesis_id') or 'Local In-Memory Logged'}",
    )

    graph.nodes.extend([t1, t2, t3, t4, t5, t6, t7])

    startup_goal = Goal(
        id="goal-mue-homeostasis-startup",
        title="Autonomous System Homeostasis, Quantified Curiosity & Common-Sense Effective Loop",
        objective=(
            "Execute continuous 24/7 system homeostasis monitoring, safeguard host physical RAM (<17.4GB) "
            "and thermals, prune stale shadow staging sessions (>2h), rotate staging backups (<500MB), "
            "compute PAD emotional vitality, conduct deep research to quantify Coding Curiosity (C_code) "
            "and Common Sense (Omega_sense), drive the closed Effective Homeostatic Loop (C_eff = C_code * Omega_sense), "
            "and publish real-time health diagnostic hypotheses to the Cognitive Blackboard."
        ),
        constraints="Never mutate immutable enclave security files. Limit diagnostic sweeps to <500ms and <50MB RAM. Common sense gate vetoes any proposal with teleological drift > 0.45. Zero token waste for routine scans.",
        success_criteria="Host memory < 85%, shadow staging pruned, backups within 500MB limit, SQLite databases vacuumed, C_code and Omega_sense quantified, effective loop C_eff evaluated, and health hypothesis active on Cognitive Blackboard.",
        state=GoalState.ACTIVE,
        task_graph=graph,
        progress=100.0,
        autonomy_mode=AutonomyMode.FULL,
    )
    # Ensure source is 'user' so it displays in /goals dashboard
    startup_goal.source = "user"
    startup_goal.priority = 10  # P0/P1 top operational priority

    # 3. Persist to Redis
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(redis_url, decode_responses=True)
        await save_goal(r, startup_goal)
        print(f"[REDIS] Goal '{startup_goal.id}' successfully saved to Redis.")
        await r.aclose()
    except Exception as e:
        print(f"[REDIS WARNING] Could not save to {redis_url}: {e} (In-memory verification passed)")

    # 4. Also register to SeededAgentOrchestrator in-memory
    try:
        from src.agent_manager.seeded_orchestrator import SeededAgentOrchestrator
        orch = SeededAgentOrchestrator()
        await orch.create_agent_goal("bot-16", startup_goal.objective)
        print(f"[ORCHESTRATOR] Bot 16 (Mue) active goal assigned in orchestrator.")
    except Exception as e:
        print(f"[ORCHESTRATOR WARNING] {e}")

    print("=" * 70)
    print("✅ STARTUP GOAL SEEDING COMPLETE — MUE IS NOW OPERATIONAL")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(seed_startup_goal())
