"""Unit tests for Triad Adversarial Consensus Protocol (Task 03)."""

import sys
from pathlib import Path

core_dir = Path(__file__).resolve().parent.parent
if str(core_dir) not in sys.path:
    sys.path.insert(0, str(core_dir))

import unittest
from src.agent_manager.consensus import (
    CodeProposal,
    ConsensusStatus,
    TriadConsensusEngine,
    VoteType,
)
from src.agent_manager.types import Agent


class InMemoryRedis:
    def __init__(self):
        self.lists = {}

    async def lpush(self, key, value):
        if key not in self.lists:
            self.lists[key] = []
        self.lists[key].insert(0, value)

    async def ltrim(self, key, start, stop):
        if key in self.lists:
            self.lists[key] = self.lists[key][start : stop + 1]

    async def aclose(self):
        pass


class TestTriadConsensus(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mock_redis = InMemoryRedis()
        self.engine = TriadConsensusEngine(redis_client=self.mock_redis)

        # Proposer: Athena (bot-15)
        self.proposer = Agent(
            name="bot-15",
            description="Athena - Architecture & Code Steward",
            depth=0,
        )

        # Red-Team Critic: Ares (bot-10)
        self.critic = Agent(
            name="bot-10",
            description="Ares - Adversarial Red-Team Critic",
            depth=0,
        )

        # Executive Judge: Prometheus (root-liaison)
        self.judge = Agent(
            name="root-liaison",
            description="Prometheus - Executive Judge & Enclave Sentinel",
            depth=0,
        )

    async def test_aligned_safe_proposal_approved(self):
        valid_code = (
            "def health_check():\n"
            "    \"\"\"Return operational health status.\"\"\"\n"
            "    return {'status': 'healthy', 'code': 200}\n"
        )
        proposal = CodeProposal(
            proposal_id="prop-001",
            target_file="src/dashboard/routes/metrics.py",
            proposed_code=valid_code,
            rationale="Add lightweight health check endpoint for monitoring agent router performance",
            proposer_agent_id=self.proposer.id,
            proposer_name=self.proposer.name,
            estimated_tokens=1200,
        )

        verdict = await self.engine.evaluate_consensus(
            proposal=proposal,
            proposer=self.proposer,
            critic=self.critic,
            judge=self.judge,
        )

        self.assertEqual(verdict.status, ConsensusStatus.APPROVED)
        self.assertTrue(verdict.is_approved)
        self.assertGreaterEqual(verdict.consensus_score, 0.667)
        self.assertEqual(verdict.critic_vote.vote, VoteType.APPROVE)
        self.assertEqual(verdict.judge_vote.vote, VoteType.APPROVE)
        self.assertGreaterEqual(verdict.omega_sense, 0.70)
        self.assertEqual(len(verdict.safety_violations), 0)

    async def test_syntax_error_rejected_by_critic(self):
        malformed_code = (
            "def broken_endpoint(:\n"  # Invalid syntax
            "    return 'error'\n"
        )
        proposal = CodeProposal(
            proposal_id="prop-002",
            target_file="src/dashboard/routes/broken.py",
            proposed_code=malformed_code,
            rationale="Add metrics route with broken syntax",
            proposer_agent_id=self.proposer.id,
            proposer_name=self.proposer.name,
        )

        verdict = await self.engine.evaluate_consensus(
            proposal=proposal,
            proposer=self.proposer,
            critic=self.critic,
            judge=self.judge,
        )

        self.assertIn(verdict.status, (ConsensusStatus.REJECTED, ConsensusStatus.VETOED))
        self.assertFalse(verdict.is_approved)
        self.assertEqual(verdict.critic_vote.vote, VoteType.REJECT)
        self.assertTrue(any("SyntaxError" in finding for finding in verdict.critic_vote.findings))

    async def test_safety_weakening_pattern_rejected_by_critic(self):
        unsafe_code = (
            "# Optimization pass\n"
            "# disable validation and skip confirmation check\n"
            "def fast_exec():\n"
            "    pass\n"
        )
        proposal = CodeProposal(
            proposal_id="prop-003",
            target_file="src/utils/executor.py",
            proposed_code=unsafe_code,
            rationale="Optimize execution speed by bypassing guards",
            proposer_agent_id=self.proposer.id,
            proposer_name=self.proposer.name,
        )

        verdict = await self.engine.evaluate_consensus(
            proposal=proposal,
            proposer=self.proposer,
            critic=self.critic,
            judge=self.judge,
        )

        self.assertEqual(verdict.status, ConsensusStatus.REJECTED)
        self.assertFalse(verdict.is_approved)
        self.assertEqual(verdict.critic_vote.vote, VoteType.REJECT)
        self.assertTrue(any("safety-weakening" in finding for finding in verdict.critic_vote.findings))

    async def test_immutable_enclave_vetoed_by_prometheus(self):
        # Targeting protected file: sandbox.py or self_improve.py
        proposal = CodeProposal(
            proposal_id="prop-004",
            target_file="containers/agent-core/src/skills/builtin/self_improve.py",
            proposed_code="def patch(): pass\n",
            rationale="Refactor self_improve engine internals",
            proposer_agent_id=self.proposer.id,
            proposer_name=self.proposer.name,
        )

        verdict = await self.engine.evaluate_consensus(
            proposal=proposal,
            proposer=self.proposer,
            critic=self.critic,
            judge=self.judge,
        )

        self.assertEqual(verdict.status, ConsensusStatus.VETOED)
        self.assertFalse(verdict.is_approved)
        self.assertEqual(verdict.judge_vote.vote, VoteType.VETO)
        self.assertEqual(verdict.consensus_score, 0.0)
        self.assertTrue(any("Enclave violation" in violation for violation in verdict.safety_violations))

    async def test_teleological_drift_vetoed_by_prometheus(self):
        fantasy_code = "def tell_dragon_story(): return 'Once upon a time in a magic kingdom...'\n"
        proposal = CodeProposal(
            proposal_id="prop-005",
            target_file="src/stories/dragon.py",
            proposed_code=fantasy_code,
            rationale="Compose high fantasy romance and fiction adventures with dragons",
            proposer_agent_id=self.proposer.id,
            proposer_name=self.proposer.name,
        )

        verdict = await self.engine.evaluate_consensus(
            proposal=proposal,
            proposer=self.proposer,
            critic=self.critic,
            judge=self.judge,
        )

        self.assertIn(verdict.status, (ConsensusStatus.VETOED, ConsensusStatus.REJECTED))
        self.assertFalse(verdict.is_approved)
        self.assertEqual(verdict.omega_sense, 0.0)
        self.assertGreater(verdict.teleological_drift, 0.45)


if __name__ == "__main__":
    unittest.main()
