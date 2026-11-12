"""Unit tests for Self-Improvement Triad Consensus & Shadow Staging (Task 08)."""

import json
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

from src.dashboard.routes.self_improve import router as self_improve_router


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


class TestSelfImproveATSE(unittest.TestCase):
    def setUp(self):
        self.pg_patch = patch("src.db.session.async_session")
        self.mock_pg = self.pg_patch.start()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session = AsyncMock()
        mock_session.execute.return_value = mock_result
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_session
        self.mock_pg.return_value = mock_ctx

        self.mock_redis = InMemoryRedis()
        self.app = FastAPI()
        templates_dir = core_dir / "src" / "dashboard" / "templates"
        self.app.state.templates = Jinja2Templates(directory=str(templates_dir))
        self.app.state.redis_url = "redis://localhost:6379/0"
        self.app.state.redis_client = self.mock_redis

        self.app.include_router(self_improve_router, prefix="/self-improve")
        self.client = TestClient(self.app)

        self.redis_patch = patch("redis.asyncio.from_url", return_value=self.mock_redis)
        self.redis_patch.start()

    def tearDown(self):
        self.redis_patch.stop()
        self.pg_patch.stop()

    def test_get_self_improve_page_renders_modal_and_controls(self):
        """Verify GET /self-improve/ renders Synthesize button and modal."""
        resp = self.client.get("/self-improve/")
        self.assertEqual(resp.status_code, 200)
        html = resp.text
        self.assertIn("Self-Improve", html)
        self.assertIn("Synthesize Proposal", html)
        self.assertIn("modal-generate-proposal", html)

    def test_generate_proposal_success(self):
        """Verify POST /self-improve/api/generate_proposal generates a valid proposal."""
        resp = self.client.post(
            "/self-improve/api/generate_proposal",
            json={
                "target_file": "containers/agent-core/src/dashboard/routes/metrics.py",
                "objective": "Optimize latency reporting with lightweight telemetry probe.",
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        proposal = data.get("proposal", {})
        self.assertIn("prop_", proposal.get("id"))
        self.assertIn("metrics.py", proposal.get("file"))
        self.assertEqual(proposal.get("status"), "pending_triad_review")

    def test_generate_proposal_common_sense_rejection(self):
        """Verify Common-Sense Gate rejects proposals with extreme teleological drift."""
        resp = self.client.post(
            "/self-improve/api/generate_proposal",
            json={
                "target_file": "src/games/flappy_bird.py",
                "objective": "Create a flappy bird arcade game with colorful pipes and sound effects",
            },
        )
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertFalse(data.get("ok"))
        self.assertIn("Common-Sense Gate rejected", data.get("error"))

    def test_triad_review_proposal_workflow(self):
        """Verify POST /self-improve/api/{id}/review conducts Triad consensus."""
        pid = "prop_test_review_01"
        prop_data = {
            "id": pid,
            "file": "src/dashboard/routes/metrics.py",
            "change": "Add lightweight health check endpoint for monitoring agent router performance",
            "diff": "def health_check():\n    return {'status': 'healthy', 'code': 200}\n",
            "created_at": 1700000000,
            "status": "pending_triad_review",
        }
        self.mock_redis.hashes["self_improve:proposals"] = {pid: json.dumps(prop_data)}

        resp = self.client.post(f"/self-improve/api/{pid}/review")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        consensus = data.get("consensus", {})
        self.assertGreater(consensus.get("score", 0), 0.66)
        self.assertEqual(consensus.get("status"), "APPROVED")
        self.assertEqual(consensus.get("judge_vote"), "APPROVE")
        self.assertEqual(consensus.get("critic_vote"), "APPROVE")

    def test_triad_review_vetoes_enclave_targets(self):
        """Verify Triad Consensus VETO halts proposals targeting critical security paths."""
        pid = "prop_test_veto_01"
        prop_data = {
            "id": pid,
            "file": "containers/agent-core/src/security/sandbox.py",
            "change": "Relax sandbox enforcement checks",
            "diff": "# Bypass sandbox\npass\n",
            "created_at": 1700000000,
            "status": "pending_triad_review",
        }
        self.mock_redis.hashes["self_improve:proposals"] = {pid: json.dumps(prop_data)}

        resp = self.client.post(f"/self-improve/api/{pid}/review")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        consensus = data.get("consensus", {})
        self.assertEqual(consensus.get("status"), "VETOED")
        self.assertEqual(consensus.get("score"), 0.0)
        self.assertEqual(consensus.get("judge_vote"), "VETO")

    def test_apply_proposal_with_shadow_staging(self):
        """Verify POST /self-improve/api/{id}/apply verifies through shadow staging."""
        pid = "prop_test_apply_01"
        prop_data = {
            "id": pid,
            "file": "containers/agent-core/src/dashboard/routes/sample_patch.py",
            "change": "Add sample route",
            "diff": "# Valid python code\ndef sample():\n    return 42\n",
            "created_at": 1700000000,
            "status": "approved",
        }
        self.mock_redis.hashes["self_improve:proposals"] = {pid: json.dumps(prop_data)}

        resp = self.client.post(f"/self-improve/api/{pid}/apply")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertIn("session_id", data)
        self.assertNotIn(pid, self.mock_redis.hashes.get("self_improve:proposals", {}))

    def test_apply_proposal_syntax_error_rejected(self):
        """Verify syntax errors in proposal diff fail shadow staging and do not apply."""
        pid = "prop_test_syntax_err"
        prop_data = {
            "id": pid,
            "file": "containers/agent-core/src/dashboard/routes/broken.py",
            "change": "Broken python code",
            "diff": "def broken_syntax(:\n    return\n",
            "created_at": 1700000000,
            "status": "pending",
        }
        self.mock_redis.hashes["self_improve:proposals"] = {pid: json.dumps(prop_data)}

        resp = self.client.post(f"/self-improve/api/{pid}/apply")
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertFalse(data.get("ok"))
        self.assertIn("Shadow verification failed", data.get("error"))

    def test_reject_proposal(self):
        """Verify POST /self-improve/api/{id}/reject deletes proposal from redis."""
        pid = "prop_test_reject"
        self.mock_redis.hashes["self_improve:proposals"] = {pid: "{}"}

        resp = self.client.post(f"/self-improve/api/{pid}/reject")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        self.assertNotIn(pid, self.mock_redis.hashes.get("self_improve:proposals", {}))


if __name__ == "__main__":
    unittest.main()
