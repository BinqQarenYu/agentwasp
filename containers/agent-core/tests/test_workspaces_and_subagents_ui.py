"""Unit tests for Workspaces AST Semantic Query & Subagent Genesis UI/Endpoints."""

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

core_dir = Path(__file__).resolve().parent.parent
if str(core_dir) not in sys.path:
    sys.path.insert(0, str(core_dir))

from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.templating import Jinja2Templates

from src.dashboard.routes.workspaces import router as workspaces_router
from src.dashboard.routes.agents import router as agents_router
from src.agent_manager.seeded_orchestrator import SeededAgentOrchestrator
from src.agent_manager.types import Agent, AgentStatus
from src.goal_orchestrator.types import AutonomyMode, CognitiveBudget


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


class TestWorkspacesAndSubagentsUI(unittest.TestCase):
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

        self.orchestrator = SeededAgentOrchestrator()
        self.app.state.agent_orchestrator = self.orchestrator

        self.app.include_router(workspaces_router, prefix="/workspaces")
        self.app.include_router(agents_router, prefix="/agents")
        self.client = TestClient(self.app)

    def tearDown(self):
        self.pg_patch.stop()

    # -------------------------------------------------------------------------
    # Workspaces Tests
    # -------------------------------------------------------------------------
    def test_get_workspaces_page_renders_modal_and_buttons(self):
        """Verify GET /workspaces renders AST Query Modal and buttons."""
        resp = self.client.get("/workspaces/")
        self.assertEqual(resp.status_code, 200)
        html = resp.text
        self.assertIn("Codebases & Workspaces", html)
        self.assertIn("modal-query-graph", html)
        self.assertIn("btn-rescan-graph", html)
        self.assertIn("btn-open-query-modal", html)
        self.assertIn("Query Graph", html)

    def test_query_ast_graph_endpoint(self):
        """Verify POST /workspaces/api/query returns scored AST nodes."""
        resp = self.client.post("/workspaces/api/query", json={"query": "teleology"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertGreaterEqual(data.get("total_matches", 0), 1)
        results = data.get("results", [])
        self.assertTrue(len(results) > 0)
        top = results[0]
        self.assertIn("score", top)
        self.assertIn("id", top)
        self.assertIn("name", top)
        self.assertIn("in_degree", top)
        self.assertIn("out_degree", top)

    def test_query_ast_graph_type_filtering(self):
        """Verify POST /workspaces/api/query honors type filters."""
        resp = self.client.post("/workspaces/api/query", json={"query": "", "type_filter": "function", "limit": 10})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        results = data.get("results", [])
        for r in results:
            self.assertEqual(r.get("type"), "function")

    def test_rescan_ast_graph_endpoint(self):
        """Verify POST /workspaces/api/rescan computes entropy and node count."""
        resp = self.client.post("/workspaces/api/rescan")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertGreater(data.get("node_count", 0), 0)
        self.assertGreater(data.get("edge_count", 0), 0)
        self.assertGreater(data.get("codebase_entropy", 0.0), 0.0)
        self.assertIn("synced_at", data)

    # -------------------------------------------------------------------------
    # Subagents Genesis & UI Tests
    # -------------------------------------------------------------------------
    def test_get_agents_page_renders_depth_badges(self):
        """Verify GET /agents/ displays Depth badges and Spawn buttons."""
        resp = self.client.get("/agents/")
        self.assertEqual(resp.status_code, 200)
        html = resp.text
        self.assertIn("Depth 0 (Root)", html)
        self.assertIn("modal-spawn-subagent", html)
        self.assertIn("modal-reap-subagent", html)
        self.assertIn("btn-spawn-subagent", html)

    def test_spawn_and_reap_subagent_lifecycle(self):
        """Test full spawn, task objective assignment, and reap lifecycle."""
        # 1. Spawn subagent from root-liaison (Prometheus)
        spawn_resp = self.client.post(
            "/agents/api/root-liaison/spawn_subagent",
            json={
                "role": "Security Auditor",
                "task_objective": "Audit open API endpoints for CSRF protection.",
                "timeout_seconds": 120,
                "token_budget": 1500,
            },
        )
        self.assertEqual(spawn_resp.status_code, 200)
        data = spawn_resp.json()
        self.assertTrue(data.get("ok"))
        subagent = data.get("subagent", {})
        subagent_id = subagent.get("id")
        self.assertEqual(subagent.get("depth"), 1)
        self.assertTrue(subagent.get("is_ephemeral"))
        self.assertEqual(subagent.get("parent_agent_id"), "root-liaison")
        self.assertIn("sub_Prometheus", subagent.get("name"))

        # Verify parent tracked subagent
        parent = self.orchestrator._agents["root-liaison"]
        self.assertIn(subagent_id, parent.spawned_subagents)

        # 2. Reap subagent
        reap_resp = self.client.post(
            "/agents/api/root-liaison/reap_subagent",
            json={
                "subagent_id": subagent_id,
                "reason": "security_audit_completed",
            },
        )
        self.assertEqual(reap_resp.status_code, 200)
        reap_data = reap_resp.json()
        self.assertTrue(reap_data.get("ok"))
        self.assertEqual(reap_data.get("reaped"), subagent_id)
        self.assertNotIn(subagent_id, parent.spawned_subagents)

    def test_spawn_subagent_depth_limit_violation(self):
        """Verify that Depth-1 subagents are strictly prohibited from spawning children."""
        # First spawn a child
        spawn_resp = self.client.post(
            "/agents/api/bot-15/spawn_subagent",
            json={
                "role": "Junior Proposer",
                "task_objective": "Formulate a tentative micro-proposal.",
            },
        )
        self.assertEqual(spawn_resp.status_code, 200)
        child_id = spawn_resp.json()["subagent"]["id"]

        # Attempt to spawn a nested child from the depth-1 agent
        nested_resp = self.client.post(
            f"/agents/api/{child_id}/spawn_subagent",
            json={
                "role": "Nested Child",
                "task_objective": "Violate depth-1 limit.",
            },
        )
        self.assertEqual(nested_resp.status_code, 400)
        data = nested_resp.json()
        self.assertFalse(data.get("ok"))
        self.assertIn("Depth limit exceeded", data.get("error"))

    def test_spawn_subagent_concurrency_limit(self):
        """Verify that parents cannot exceed their maximum subagent allocation."""
        parent = self.orchestrator._agents["bot-10"]  # Ares
        parent.max_subagents = 2
        parent.spawned_subagents = ["dummy-child-1", "dummy-child-2"]

        spawn_resp = self.client.post(
            "/agents/api/bot-10/spawn_subagent",
            json={
                "role": "Adversary Critic",
                "task_objective": "Test concurrency cap.",
            },
        )
        self.assertEqual(spawn_resp.status_code, 400)
        data = spawn_resp.json()
        self.assertFalse(data.get("ok"))
        self.assertIn("Concurrency limit exceeded", data.get("error"))

    def test_spawn_subagent_hardware_safety_guard(self):
        """Verify local Ollama models are downgraded to llama3.2:latest on CPU-only PC."""
        spawn_resp = self.client.post(
            "/agents/api/root-liaison/spawn_subagent",
            json={
                "role": "Heavy Model Tester",
                "task_objective": "Ensure safe model sizing.",
                "model_provider": "ollama",
                "model_name": "llama3:70b-instruct-q4_0",
            },
        )
        self.assertEqual(spawn_resp.status_code, 200)
        subagent = spawn_resp.json()["subagent"]
        self.assertEqual(subagent.get("model_name"), "llama3.2:latest")


if __name__ == "__main__":
    unittest.main()
