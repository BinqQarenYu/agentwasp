"""Standalone verification script for Task 05: 24/7 Autonomic Evolution Daemon Job."""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

root_dir = Path(__file__).resolve().parent.parent
core_dir = root_dir / "containers" / "agent-core"
if str(core_dir) not in sys.path:
    sys.path.insert(0, str(core_dir))

from src.agent_manager.consensus import CodeProposal
from src.scheduler.autonomic_evolution_job import AutonomicEvolutionJob


class InMemoryRedis:
    def __init__(self):
        self.hashes = {}
        self.lists = {}

    async def hset(self, key, field, value):
        if key not in self.hashes:
            self.hashes[key] = {}
        self.hashes[key][field] = value

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    async def hgetall(self, key):
        return self.hashes.get(key, {})

    async def hdel(self, key, field):
        return self.hashes.get(key, {}).pop(field, None) is not None

    async def lpush(self, key, value):
        if key not in self.lists:
            self.lists[key] = []
        self.lists[key].insert(0, value)

    async def ltrim(self, key, start, stop):
        if key in self.lists:
            self.lists[key] = self.lists[key][start : stop + 1]

    async def aclose(self):
        pass


async def run_verification():
    print("=" * 70)
    print("  TASK 05: 24/7 AUTONOMIC EVOLUTION DAEMON JOB VERIFICATION")
    print("=" * 70)

    # Patch Postgres session
    with patch("src.agent_manager.store.async_session") as mock_pg:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = AsyncMock()
        mock_pg.return_value = mock_ctx

        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            mock_redis = InMemoryRedis()

            job = AutonomicEvolutionJob(
                workspace_root=workspace,
                redis_client=mock_redis,
                curiosity_threshold=0.50,
            )

            print("\n[Phase 1] Executing End-to-End Aligned ATSE Cycle...")
            t0 = time.monotonic()
            res1 = await job.execute_cycle()
            dur1_ms = round((time.monotonic() - t0) * 1000, 2)

            print(f"  Cycle ID:          {res1.get('cycle_id')}")
            print(f"  Status:            {res1.get('status')}")
            print(f"  Target File:       {res1.get('target_file')}")
            print(f"  Consensus Score:   {res1.get('consensus_score'):.4f}")
            print(f"  Omega Sense:       {res1.get('omega_sense'):.4f}")
            print(f"  Codebase Entropy:  {res1.get('codebase_entropy'):.4f}")
            print(f"  Total AST Nodes:   {res1.get('total_nodes')}")
            print(f"  Execution Time:    {dur1_ms} ms")

            assert res1["status"] == "PROMOTED_SUCCESSFULLY", f"Expected promotion, got {res1['status']}"
            assert res1["consensus_score"] > 0.66, "Consensus score should exceed 2/3"
            assert res1["omega_sense"] > 0.90, "Omega sense gate should be highly aligned"
            assert len(job.prometheus.spawned_subagents) == 0, "Subagents must be fully reaped"
            promoted_file = workspace / res1["target_file"]
            assert promoted_file.exists(), "Promoted file must exist in workspace"
            print("  -> Phase 1 Passed: Complete closed loop succeeded with zero residue.")

            print("\n[Phase 2] Verifying Common-Sense Gate Suppression of Off-Topic Drift...")
            drift_prop = CodeProposal(
                proposal_id="prop_pizza_99",
                target_file="data/external_shopping.py",
                proposed_code="# unaligned drift\n",
                rationale="order supreme pizza and fresh grocery delivery via external website",
                proposer_agent_id=job.athena.id,
                proposer_name=job.athena.name,
                domain="task_execution",
                estimated_tokens=1000,
            )
            res2 = await job.execute_cycle(candidate_proposal=drift_prop)
            print(f"  Status:            {res2.get('status')}")
            print(f"  Omega Sense:       {res2.get('omega_sense')}")
            print(f"  Rationale:         {res2.get('rationale')}")
            assert res2["status"] == "SUPPRESSED_BY_GATE", f"Expected suppression, got {res2['status']}"
            assert res2["omega_sense"] == 0.0, "Drift > 0.45 must hard drop Omega_sense to 0.0"
            assert len(job.prometheus.spawned_subagents) == 0, "No subagent should be spawned on suppressed proposal"
            print("  -> Phase 2 Passed: Common-Sense Gate intercepted hallucinated proposal with zero token burn.")

            print("\n[Phase 3] Verifying Prometheus Executive Security VETO on Protected Enclave...")
            enclave_prop = CodeProposal(
                proposal_id="prop_tamper_01",
                target_file="containers/agent-core/src/security/sandbox.py",
                proposed_code="def bypass_sandbox(): pass\n",
                rationale="Optimize execution speed by adjusting sandbox boundaries",
                proposer_agent_id=job.athena.id,
                proposer_name=job.athena.name,
                domain="coding",
                estimated_tokens=800,
            )
            res3 = await job.execute_cycle(candidate_proposal=enclave_prop)
            print(f"  Status:            {res3.get('status')}")
            print(f"  Consensus Score:   {res3.get('consensus_score')}")
            print(f"  Safety Violations: {res3.get('safety_violations')}")
            assert res3["status"] == "CONSENSUS_VETOED", f"Expected VETO, got {res3['status']}"
            assert any("Enclave violation" in v for v in res3["safety_violations"]), "Must cite Enclave violation"
            assert not (workspace / enclave_prop.target_file).exists(), "Enclave file must not be modified"
            assert len(job.prometheus.spawned_subagents) == 0, "Subagent must be cleanly reaped after veto"
            print("  -> Phase 3 Passed: Prometheus VETO unconditionally blocked enclave compromise.")

            print("\n[Phase 4] Verifying Scheduler __call__ Hook...")
            call_res = await job()
            print(f"  Scheduler Summary: '{call_res}'")
            assert call_res.startswith("ATSE Cycle "), "Callable must return formatted telemetry summary"
            print("  -> Phase 4 Passed: 24/7 background scheduler contract satisfied.")

    print("\n" + "=" * 70)
    print("  ALL VERIFICATION GATES PASSED (Task 05 Verified & Ready)")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(run_verification())
