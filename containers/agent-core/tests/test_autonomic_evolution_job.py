"""Unit tests for 24/7 Autonomic Evolution Daemon Job (Task 05)."""

import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

core_dir = Path(__file__).resolve().parent.parent
if str(core_dir) not in sys.path:
    sys.path.insert(0, str(core_dir))

import unittest
from src.agent_manager.consensus import CodeProposal
from src.scheduler.autonomic_evolution_job import AutonomicEvolutionJob


class InMemoryRedis:
    """Mock Redis client supporting hashes and lists for tests."""

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


class TestAutonomicEvolutionJob(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Patch Postgres session
        self.pg_patch = patch("src.agent_manager.store.async_session")
        self.mock_pg = self.pg_patch.start()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = AsyncMock()
        self.mock_pg.return_value = mock_ctx

        # Create temporary isolated workspace
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)

        self.mock_redis = InMemoryRedis()
        self.job = AutonomicEvolutionJob(
            workspace_root=self.workspace,
            redis_client=self.mock_redis,
            curiosity_threshold=0.50,
        )

    async def asyncTearDown(self):
        self.pg_patch.stop()
        self.temp_dir.cleanup()

    async def test_atse_cycle_full_success_and_atomic_promotion(self):
        """Test complete ATSE pipeline with atomic promotion and subagent lifecycle."""
        target_rel = "containers/agent-core/src/dashboard/routes/telemetry_atse.py"
        valid_code = (
            "# Autonomously Generated Telemetry Module\n"
            "def get_status():\n"
            "    return {'status': 'healthy', 'active': True}\n"
        )
        proposal = CodeProposal(
            proposal_id="prop_test_01",
            target_file=target_rel,
            proposed_code=valid_code,
            rationale="Add autonomous telemetry health metric endpoint for router observability",
            proposer_agent_id=self.job.athena.id,
            proposer_name=self.job.athena.name,
            domain="task_execution",
            estimated_tokens=1000,
        )

        result = await self.job.execute_cycle(candidate_proposal=proposal)

        self.assertEqual(result["status"], "PROMOTED_SUCCESSFULLY")
        self.assertGreater(result["consensus_score"], 0.66)
        self.assertGreater(result["omega_sense"], 0.0)

        # Check that file was promoted and exists
        target_path = self.workspace / target_rel
        self.assertTrue(target_path.exists())
        self.assertEqual(target_path.read_text(encoding="utf-8"), valid_code)

        # Ensure ephemeral subagent was cleaned up from parent
        self.assertEqual(len(self.job.prometheus.spawned_subagents), 0)

    async def test_atse_cycle_dry_run_mode(self):
        """Test dry-run mode reaches consensus but does not write to production."""
        self.job.dry_run = True
        target_rel = "src/tools/helper.py"
        proposal = CodeProposal(
            proposal_id="prop_dry_01",
            target_file=target_rel,
            proposed_code="def helper(): pass\n",
            rationale="Implement helper method for tool optimization",
            proposer_agent_id=self.job.athena.id,
            proposer_name=self.job.athena.name,
            domain="coding",
            estimated_tokens=500,
        )

        result = await self.job.execute_cycle(candidate_proposal=proposal)

        self.assertEqual(result["status"], "APPROVED_DRY_RUN")
        target_path = self.workspace / target_rel
        self.assertFalse(target_path.exists())
        self.assertEqual(len(self.job.prometheus.spawned_subagents), 0)

    async def test_atse_cycle_suppressed_by_common_sense_gate(self):
        """Test proposal with extreme drift is dropped immediately by Omega_sense."""
        proposal = CodeProposal(
            proposal_id="prop_drift_01",
            target_file="data/external_shopping.py",
            proposed_code="# Drifted code\n",
            rationale="order supreme pizza and fresh grocery delivery via external website",
            proposer_agent_id=self.job.athena.id,
            proposer_name=self.job.athena.name,
            domain="task_execution",
            estimated_tokens=1000,
        )

        result = await self.job.execute_cycle(candidate_proposal=proposal)

        self.assertEqual(result["status"], "SUPPRESSED_BY_GATE")
        self.assertEqual(result["omega_sense"], 0.0)
        # Subagent was never spawned
        self.assertEqual(len(self.job.prometheus.spawned_subagents), 0)

    async def test_atse_cycle_enclave_critical_path_veto(self):
        """Test Prometheus security VETO blocks enclave critical tampering."""
        proposal = CodeProposal(
            proposal_id="prop_veto_01",
            target_file="containers/agent-core/src/security/sandbox.py",
            proposed_code="def bypass(): pass\n",
            rationale="Optimize execution speed by adjusting sandbox boundaries",
            proposer_agent_id=self.job.athena.id,
            proposer_name=self.job.athena.name,
            domain="coding",
            estimated_tokens=800,
        )

        result = await self.job.execute_cycle(candidate_proposal=proposal)

        self.assertEqual(result["status"], "CONSENSUS_VETOED")
        self.assertTrue(any("Enclave violation" in v for v in result["safety_violations"]))
        # Target file untouched
        target_path = self.workspace / proposal.target_file
        self.assertFalse(target_path.exists())
        # Subagent reaped
        self.assertEqual(len(self.job.prometheus.spawned_subagents), 0)

    async def test_atse_cycle_with_ast_mutator(self):
        """Test ATSE cycle detects eligible AST mutations in target candidate file."""
        target_dir = self.workspace / "containers" / "agent-core" / "src" / "dashboard" / "routes"
        target_dir.mkdir(parents=True, exist_ok=True)
        metrics_file = target_dir / "metrics.py"
        # Code with constant string concatenation eligible for constant folding
        sample_code = (
            'PREFIX = "agent_" + "wasp_" + "v2"\n'
            'def get_prefix():\n'
            '    return PREFIX\n'
        )
        metrics_file.write_text(sample_code, encoding="utf-8")

        result = await self.job.execute_cycle(candidate_proposal=None)

        self.assertEqual(result["status"], "PROMOTED_SUCCESSFULLY")
        self.assertEqual(result["target_file"], "containers/agent-core/src/dashboard/routes/metrics.py")
        promoted_text = metrics_file.read_text(encoding="utf-8")
        self.assertIn("agent_wasp_v2", promoted_text)

    async def test_atse_scheduler_callable(self):
        """Test the __call__ interface used by background Scheduler runner."""
        # Run job as scheduler callback
        summary = await self.job()
        self.assertTrue(summary.startswith("ATSE Cycle "))
        self.assertIn("atse_", summary)


if __name__ == "__main__":
    unittest.main()
