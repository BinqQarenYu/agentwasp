"""Verification script for Task 02: Bounded Subagent Genesis System (Depth-1 Spawner).

Demonstrates and verifies:
1. Prometheus ('root-liaison') spawning a specialized 'Code Auditor' ephemeral subagent.
2. Athena ('bot-15') spawning a specialized 'Repository Watcher' ephemeral subagent.
3. Strict Depth-1 enforcement: Ephemeral subagents cannot spawn further subagents.
4. Concurrency limit capping.
5. Automatic hardware fit safety for local models.
6. Ephemeral lifecycle workflow and complete cleanup.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

core_dir = Path(__file__).resolve().parent.parent / "containers" / "agent-core"
if str(core_dir) not in sys.path:
    sys.path.insert(0, str(core_dir))

from unittest.mock import patch, AsyncMock
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


async def run_verification():
    print("[TASK-02] Starting Bounded Subagent Genesis (Depth-1 Spawner) Verification...")

    with patch("src.agent_manager.store.async_session") as mock_session:
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = AsyncMock()
        mock_session.return_value = mock_ctx

        mock_redis = InMemoryRedis()
        genesis = SubagentGenesis(redis_client=mock_redis)

        # -------------------------------------------------------------
        # 1. Prometheus Genesis Check
        # -------------------------------------------------------------
        print("\n--- Step 1: Prometheus ('root-liaison') Spawns Subagent ---")
        prometheus = Agent(
            name="root-liaison",
            description="Prometheus - Autonomous Core Operations",
            depth=0,
            max_subagents=3,
            allowed_capabilities=["safe", "monitored", "ast_ingest"],
        )

        sub_auditor = await genesis.spawn_subagent(
            parent=prometheus,
            role="AST Route Auditor",
            task_objective="Audit containers/agent-core/src/api for deprecated route handlers",
            skills_needed=["read_file", "system_probe"],
            timeout_seconds=90,
            token_budget=1500,
        )

        print(f"Spawned Subagent: {sub_auditor.name}")
        print(f"  ID: {sub_auditor.id}")
        print(f"  Parent ID: {sub_auditor.parent_agent_id}")
        print(f"  Depth: {sub_auditor.depth}")
        print(f"  Is Ephemeral: {sub_auditor.is_ephemeral}")
        print(f"  Cognitive Budget (Tokens): {sub_auditor.cognitive_budget.max_tokens_planning}")
        print(f"  Max Runtime: {sub_auditor.cognitive_budget.max_runtime_seconds}s")
        print(f"  Parent Active Subagents Count: {len(prometheus.spawned_subagents)}")

        assert sub_auditor.depth == 1
        assert sub_auditor.is_ephemeral is True
        assert sub_auditor.parent_agent_id == prometheus.id
        assert sub_auditor.id in prometheus.spawned_subagents
        print("[PASS] Step 1: Prometheus successfully spawned a bounded ephemeral subagent.")

        # -------------------------------------------------------------
        # 2. Athena Genesis Check
        # -------------------------------------------------------------
        print("\n--- Step 2: Athena ('bot-15') Spawns Subagent ---")
        athena = Agent(
            name="bot-15",
            description="Athena - Security Guardian & Fleet Auditor",
            depth=0,
            max_subagents=2,
            allowed_capabilities=["safe", "security_audit"],
        )

        sub_security = await genesis.spawn_subagent(
            parent=athena,
            role="Vulnerability Scanner",
            task_objective="Scan repo pull requests for unauthorized env var leakage",
            skills_needed=["read_file", "security_probe"],
            timeout_seconds=60,
        )

        print(f"Spawned Subagent: {sub_security.name}")
        print(f"  Parent ID: {sub_security.parent_agent_id}")
        print(f"  Depth: {sub_security.depth}")
        print(f"  Is Ephemeral: {sub_security.is_ephemeral}")
        print(f"  Athena Active Subagents Count: {len(athena.spawned_subagents)}")

        assert sub_security.depth == 1
        assert sub_security.parent_agent_id == athena.id
        print("[PASS] Step 2: Athena successfully spawned a security subagent.")

        # -------------------------------------------------------------
        # 3. Depth-1 Boundary Enforcement (Prohibit Recursive Spawning)
        # -------------------------------------------------------------
        print("\n--- Step 3: Depth-1 Boundary Enforcement ---")
        try:
            await genesis.spawn_subagent(
                parent=sub_auditor,
                role="Illegal Grandchild",
                task_objective="Attempting recursive bot generation",
            )
            raise AssertionError("Depth-1 violation was NOT caught!")
        except DepthLimitExceededError as e:
            print(f"[BLOCKED AS EXPECTED] Depth-1 Guard triggered: {e}")
            print("[PASS] Step 3: Subagent forbidden from spawning further child agents.")

        # -------------------------------------------------------------
        # 4. Concurrency Limit Capping
        # -------------------------------------------------------------
        print("\n--- Step 4: Concurrency Limit Enforcement ---")
        sub_athena2 = await genesis.spawn_subagent(
            parent=athena,
            role="Dependency Checker",
            task_objective="Check pip dependency trees",
        )
        print(f"Athena subagents count after 2nd spawn: {len(athena.spawned_subagents)}")
        assert len(athena.spawned_subagents) == 2

        try:
            await genesis.spawn_subagent(
                parent=athena,
                role="Excess Worker",
                task_objective="Attempting to exceed max_subagents (2)",
            )
            raise AssertionError("Concurrency limit was NOT caught!")
        except ConcurrencyLimitExceededError as e:
            print(f"[BLOCKED AS EXPECTED] Concurrency Leash triggered: {e}")
            print("[PASS] Step 4: Concurrency limit strictly enforced.")

        # -------------------------------------------------------------
        # 5. Local Hardware Sizing Guard
        # -------------------------------------------------------------
        print("\n--- Step 5: Hardware-Fitted Model Guard ---")
        heavy_sub = await genesis.spawn_subagent(
            parent=prometheus,
            role="Heavy Reasoner",
            task_objective="Heavy reasoning task",
            model_provider="ollama",
            model_name="deepseek-r1:70b",
        )
        print(f"Requested: ollama/deepseek-r1:70b -> Effective Model: {heavy_sub.model_name}")
        assert heavy_sub.model_name == "llama3.2:latest"
        print("[PASS] Step 5: Oversized local model automatically fitted to host PC hardware.")

        # -------------------------------------------------------------
        # 6. Ephemeral Reaping & Lifecycle Completion
        # -------------------------------------------------------------
        print("\n--- Step 6: Ephemeral Lifecycle Reaping ---")
        await genesis.reap_subagent(parent=prometheus, subagent_id=sub_auditor.id, reason="audit_completed")
        await genesis.reap_subagent(parent=prometheus, subagent_id=heavy_sub.id, reason="reasoning_done")
        await genesis.reap_subagent(parent=athena, subagent_id=sub_security.id, reason="scan_clean")
        await genesis.reap_subagent(parent=athena, subagent_id=sub_athena2.id, reason="dependencies_verified")

        print(f"Prometheus remaining subagents: {len(prometheus.spawned_subagents)}")
        print(f"Athena remaining subagents: {len(athena.spawned_subagents)}")
        assert len(prometheus.spawned_subagents) == 0
        assert len(athena.spawned_subagents) == 0
        print("[PASS] Step 6: All ephemeral subagents cleanly reaped; zero dangling processes.")

    print("\n=======================================================")
    print("ALL TASK 02 VERIFICATION CHECKS COMPLETED SUCCESSFULLY!")
    print("=======================================================")


if __name__ == "__main__":
    asyncio.run(run_verification())
