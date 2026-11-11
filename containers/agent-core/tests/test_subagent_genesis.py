"""Unit tests for Bounded Subagent Genesis System (Task 02)."""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

core_dir = Path(__file__).resolve().parent.parent
if str(core_dir) not in sys.path:
    sys.path.insert(0, str(core_dir))

import unittest
from src.agent_manager.genesis import SubagentGenesis
from src.agent_manager.types import (
    Agent,
    AgentStatus,
    ConcurrencyLimitExceededError,
    DepthLimitExceededError,
)


class InMemoryRedis:
    def __init__(self):
        self.hashes = {}

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

    async def aclose(self):
        pass


class TestSubagentGenesis(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Patch Postgres session to avoid live DB connection attempts in tests
        self.pg_patch = patch("src.agent_manager.store.async_session")
        self.mock_pg = self.pg_patch.start()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = AsyncMock()
        self.mock_pg.return_value = mock_ctx

        self.mock_redis = InMemoryRedis()
        self.genesis = SubagentGenesis(redis_client=self.mock_redis)

        # Root agent (Prometheus)
        self.prometheus = Agent(
            name="root-liaison",
            description="Chief Operations Orchestrator",
            depth=0,
            max_subagents=2,
            allowed_capabilities=["safe", "monitored", "ast_ingest"],
        )

    async def asyncTearDown(self):
        self.pg_patch.stop()

    async def test_root_can_spawn_depth1_subagent(self):
        self.assertTrue(self.prometheus.can_spawn_subagents())
        subagent = await self.genesis.spawn_subagent(
            parent=self.prometheus,
            role="Code Auditor",
            task_objective="Audit REST routes in api/routes/scheduler.py",
        )
        self.assertEqual(subagent.depth, 1)
        self.assertTrue(subagent.is_ephemeral)
        self.assertEqual(subagent.parent_agent_id, self.prometheus.id)
        self.assertIn(subagent.id, self.prometheus.spawned_subagents)
        self.assertEqual(len(self.prometheus.spawned_subagents), 1)

    async def test_depth1_subagent_cannot_spawn_nested(self):
        subagent = await self.genesis.spawn_subagent(
            parent=self.prometheus,
            role="AST Tracer",
            task_objective="Trace imports across containers",
        )
        self.assertFalse(subagent.can_spawn_subagents())

        with self.assertRaises(DepthLimitExceededError):
            await self.genesis.spawn_subagent(
                parent=subagent,
                role="Nested Worker",
                task_objective="Illegal nested spawn",
            )

    async def test_concurrency_leash_enforced(self):
        # max_subagents is 2
        sub1 = await self.genesis.spawn_subagent(
            parent=self.prometheus,
            role="Worker 1",
            task_objective="Subtask 1",
        )
        sub2 = await self.genesis.spawn_subagent(
            parent=self.prometheus,
            role="Worker 2",
            task_objective="Subtask 2",
        )
        self.assertEqual(len(self.prometheus.spawned_subagents), 2)
        self.assertFalse(self.prometheus.can_spawn_subagents())

        with self.assertRaises(ConcurrencyLimitExceededError):
            await self.genesis.spawn_subagent(
                parent=self.prometheus,
                role="Worker 3",
                task_objective="Exceeding quota",
            )

        # Reap one subagent and verify we can spawn again
        await self.genesis.reap_subagent(self.prometheus, sub1.id, reason="completed")
        self.assertEqual(len(self.prometheus.spawned_subagents), 1)
        self.assertTrue(self.prometheus.can_spawn_subagents())

        sub3 = await self.genesis.spawn_subagent(
            parent=self.prometheus,
            role="Worker 3",
            task_objective="Now allowed",
        )
        self.assertEqual(len(self.prometheus.spawned_subagents), 2)

    async def test_hardware_fit_downgrade(self):
        # Requesting an oversized model on Ollama must downgrade to PC-fitted llama3.2:latest
        sub = await self.genesis.spawn_subagent(
            parent=self.prometheus,
            role="Heavy Modeler",
            task_objective="Compute heavy reasoning",
            model_provider="ollama",
            model_name="llama3.3:70b",
        )
        self.assertEqual(sub.model_name, "llama3.2:latest")

    async def test_reap_subagent_archives_and_cleans_parent(self):
        sub = await self.genesis.spawn_subagent(
            parent=self.prometheus,
            role="Ephemeral Cleaner",
            task_objective="Do brief work",
        )
        self.assertIn(sub.id, self.prometheus.spawned_subagents)

        success = await self.genesis.reap_subagent(self.prometheus, sub.id, reason="work_finished")
        self.assertTrue(success)
        self.assertNotIn(sub.id, self.prometheus.spawned_subagents)


if __name__ == "__main__":
    unittest.main()
