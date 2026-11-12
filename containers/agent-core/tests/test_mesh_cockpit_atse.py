"""Unit tests for ATSE Cockpit Telemetry UI & Endpoints (Task 06)."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

core_dir = Path(__file__).resolve().parent.parent
if str(core_dir) not in sys.path:
    sys.path.insert(0, str(core_dir))

import unittest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.templating import Jinja2Templates

from src.dashboard.routes.mesh import router as mesh_router


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


class TestMeshCockpitATSE(unittest.TestCase):
    def setUp(self):
        self.pg_patch = patch("src.agent_manager.store.async_session")
        self.mock_pg = self.pg_patch.start()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = AsyncMock()
        self.mock_pg.return_value = mock_ctx

        self.mock_redis = InMemoryRedis()
        self.app = FastAPI()
        templates_dir = core_dir / "src" / "dashboard" / "templates"
        self.app.state.templates = Jinja2Templates(directory=str(templates_dir))
        self.app.state.redis_url = "redis://localhost:6379/0"
        self.app.state.redis_client = self.mock_redis
        self.app.include_router(mesh_router, prefix="/mesh")
        self.client = TestClient(self.app)

    def tearDown(self):
        self.pg_patch.stop()

    def test_atse_status_endpoint(self):
        """Test GET /mesh/api/atse/status returns full ATSE telemetry structure."""
        resp = self.client.get("/mesh/api/atse/status")
        self.assertEqual(resp.status_code, 200)

        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("architecture"), "ATSE-v1.0 (Autonomous Teleological Self-Evolution)")
        self.assertEqual(data.get("status"), "OPERATIONAL")

        # Pillar 1: Codebase AST Entropy
        ast_data = data.get("codebase_ast", {})
        self.assertAlmostEqual(ast_data.get("entropy_h"), 0.8451, places=3)
        self.assertEqual(ast_data.get("total_nodes"), 3200)
        self.assertEqual(ast_data.get("total_edges"), 5961)

        # Pillar 2: Common-Sense Gate
        gate_data = data.get("common_sense_gate", {})
        self.assertEqual(gate_data.get("status"), "GUARDED")
        self.assertEqual(gate_data.get("max_allowed_drift"), 0.45)
        self.assertGreater(gate_data.get("current_omega_sense"), 0.90)

        # Pillar 3: Subagent Genesis
        genesis_data = data.get("subagent_genesis", {})
        self.assertEqual(genesis_data.get("max_depth"), 1)
        self.assertEqual(genesis_data.get("state"), "BOUNDED")
        self.assertIn("llama3.2", genesis_data.get("hardware_model"))

        # Pillar 4: Triad Consensus
        consensus_data = data.get("triad_consensus", {})
        self.assertIn("Athena", consensus_data.get("proposer"))
        self.assertIn("Ares", consensus_data.get("critic"))
        self.assertIn("Prometheus", consensus_data.get("judge"))
        self.assertIn("VETO", consensus_data.get("security_veto"))

        # Pillar 5: Shadow Staging
        staging_data = data.get("shadow_staging", {})
        self.assertEqual(staging_data.get("status"), "ACTIVE")
        self.assertIn("< 500ms", staging_data.get("rollback_sla"))

    def test_atse_trigger_cycle_endpoint(self):
        """Test POST /mesh/api/atse/trigger-cycle executes a safe dry-run cycle."""
        resp = self.client.post("/mesh/api/atse/trigger-cycle")
        self.assertEqual(resp.status_code, 200)

        data = resp.json()
        self.assertTrue(data.get("ok"))
        cycle = data.get("cycle_result", {})
        self.assertIn(cycle.get("status"), ("APPROVED_DRY_RUN", "PROMOTED_SUCCESSFULLY"))
        self.assertGreater(cycle.get("consensus_score", 0), 0.66)
        self.assertGreater(cycle.get("omega_sense", 0), 0.90)

    def test_mesh_state_enriched_with_atse(self):
        """Test GET /mesh/api/state contains live ATSE summary object."""
        resp = self.client.get("/mesh/api/state")
        self.assertEqual(resp.status_code, 200)

        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertIn("atse", data)
        atse = data["atse"]
        self.assertTrue(atse.get("enabled"))
        self.assertAlmostEqual(atse.get("codebase_entropy"), 0.8451, places=3)
        self.assertEqual(atse.get("subagent_max_depth"), 1)
        self.assertEqual(atse.get("status"), "OPERATIONAL_24_7")

    def test_mesh_cockpit_html_renders_atse_badges_and_modal(self):
        """Test GET /mesh/ renders ATSE badges, modal, and trigger buttons."""
        resp = self.client.get("/mesh/")
        self.assertEqual(resp.status_code, 200)

        html = resp.text
        self.assertIn("stat-val-ast-h", html)
        self.assertIn("stat-val-omega", html)
        self.assertIn("stat-val-atse-daemon", html)
        self.assertIn("btn-toggle-atse", html)
        self.assertIn("atse-telemetry-modal", html)
        self.assertIn("btn-trigger-atse-cycle", html)


if __name__ == "__main__":
    unittest.main()
