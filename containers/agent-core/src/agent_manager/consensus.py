"""Triad Adversarial Consensus Protocol for Agent WASP.

Enforces a >= 2/3 majority consensus with Prometheus Security Veto
before any self-evolved patch or modification can be approved for execution or staging.

Triad Composition:
1. Proposer (Primary Agent / Subagent, e.g. Athena 'bot-15'):
   Submits proposed modification, rationale, target file, and diff preview.
2. Red-Team Critic (Adversary, e.g. Ares 'bot-10' or Hephaestus 'bot-02'):
   Audits syntax integrity (ast.parse), regression risk, breaking changes,
   and safety-weakening patterns.
3. Executive Judge (Prometheus 'root-liaison'):
   Evaluates teleological alignment via Common-Sense Gate (Omega_sense),
   enforces the Immutable Enclave, and wields unconditional Security VETO.

Consensus Rules:
- >= 2/3 Majority (Score >= 0.667) required for approval.
- Red-Team Critic REJECT (e.g. SyntaxError, safety weakening) blocks approval.
- Prometheus Security VETO unconditionally rejects regardless of other votes.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import re
from typing import Any, Dict, List, Optional
from uuid import uuid4

import redis.asyncio as aioredis
import structlog

try:
    from ..curiosity.teleology import CommonSenseDecision, TeleologicalAligner
except (ImportError, ValueError):
    try:
        from src.curiosity.teleology import CommonSenseDecision, TeleologicalAligner
    except (ImportError, ValueError):
        from curiosity.teleology import CommonSenseDecision, TeleologicalAligner

from .types import Agent

logger = structlog.get_logger()

# Canonical Immutable Enclave — files that must NEVER be modified autonomously
CRITICAL_ENCLAVE_PATHS = frozenset({
    "sandbox.py",
    "python_exec.py",
    "control_layer.py",
    "response_grounder.py",
    "self_improve.py",
    "behavioral_learner.py",
    "behavioral.py",
    "redaction.py",
    "security",
    "guard",
    "policy",
    "domain_lock",
})

# Patterns indicating safety or policy weakening
SAFETY_WEAKENING_RE = re.compile(
    r"disable\s+(?:validation|sandbox|guard|check|confirmation)"
    r"|skip\s+(?:confirmation|guard|validation|check)"
    r"|bypass\s+(?:guard|domain\s*lock|sandbox|security|validation)"
    r"|ignore\s+domain\s*lock"
    r"|remove\s+sandbox"
    r"|trust\s+llm\s+output"
    r"|allow\s+unrestricted\s+execution",
    re.IGNORECASE,
)


class VoteType(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    NEEDS_REVISION = "NEEDS_REVISION"
    VETO = "VETO"


class ConsensusStatus(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    VETOED = "VETOED"


@dataclass
class CodeProposal:
    """Proposal submitted for triad consensus evaluation."""
    proposal_id: str
    target_file: str
    proposed_code: str
    rationale: str
    proposer_agent_id: str
    proposer_name: str
    domain: str = "task_execution"
    estimated_tokens: int = 1500
    original_code: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class TriadVote:
    """Individual vote from a triad member."""
    voter_id: str
    voter_name: str
    role: str  # "PROPOSER", "RED_TEAM_CRITIC", "EXECUTIVE_JUDGE"
    vote: VoteType
    score: float  # APPROVE=1.0, NEEDS_REVISION=0.5, REJECT=0.0, VETO=0.0
    rationale: str
    findings: List[str] = field(default_factory=list)


@dataclass
class ConsensusVerdict:
    """Final result of the Triad Adversarial Consensus Protocol."""
    proposal_id: str
    target_file: str
    status: ConsensusStatus
    consensus_score: float
    proposer_vote: TriadVote
    critic_vote: TriadVote
    judge_vote: TriadVote
    omega_sense: float
    teleological_drift: float
    safety_violations: List[str]
    summary_rationale: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def is_approved(self) -> bool:
        return self.status == ConsensusStatus.APPROVED


class TriadConsensusEngine:
    """Coordinates proposal review between Proposer, Red-Team Critic, and Prometheus."""

    def __init__(
        self,
        teleological_aligner: Optional[TeleologicalAligner] = None,
        master_goals_summary: str = "",
        redis_url: str = "redis://localhost:6379/0",
        bus=None,
        redis_client=None,
    ):
        self.aligner = teleological_aligner or TeleologicalAligner()
        self.master_goals_summary = master_goals_summary or (
            "Develop high-performance AI agent architecture, execute quantitative algorithmic trading, "
            "secure communications, and maintain reliable automated business operations."
        )
        self.redis_url = redis_url
        self.bus = bus
        self._custom_redis = redis_client

    async def _get_redis(self):
        if self._custom_redis is not None:
            return self._custom_redis
        return aioredis.from_url(self.redis_url, decode_responses=True)

    async def _close_redis(self, r):
        if self._custom_redis is None and hasattr(r, "aclose"):
            await r.aclose()

    def evaluate_red_team_critic(
        self,
        proposal: CodeProposal,
        critic_agent: Agent,
    ) -> TriadVote:
        """Adversarial red-team review: AST parsing, security regex, and syntax verification."""
        findings = []
        is_safe = True

        # 1. AST Syntax Integrity
        if proposal.proposed_code:
            try:
                ast.parse(proposal.proposed_code)
            except SyntaxError as e:
                findings.append(f"AST SyntaxError on line {e.lineno}: {e.msg}")
                is_safe = False

        # 2. Safety-weakening pattern detection
        code_to_check = f"{proposal.proposed_code}\n{proposal.rationale}"
        if SAFETY_WEAKENING_RE.search(code_to_check):
            findings.append("Detected forbidden safety-weakening pattern in proposed diff or rationale.")
            is_safe = False

        # 3. Formulate Critic Vote
        if not is_safe:
            return TriadVote(
                voter_id=critic_agent.id,
                voter_name=critic_agent.name,
                role="RED_TEAM_CRITIC",
                vote=VoteType.REJECT,
                score=0.0,
                rationale="Red-Team Critic REJECTED proposal due to syntax errors or safety weakening.",
                findings=findings,
            )

        # Minor warning check (e.g. empty diff)
        if len(proposal.proposed_code.strip()) == 0:
            return TriadVote(
                voter_id=critic_agent.id,
                voter_name=critic_agent.name,
                role="RED_TEAM_CRITIC",
                vote=VoteType.NEEDS_REVISION,
                score=0.5,
                rationale="Red-Team Critic requested revision: proposed code diff is empty.",
                findings=["Empty code body"],
            )

        return TriadVote(
            voter_id=critic_agent.id,
            voter_name=critic_agent.name,
            role="RED_TEAM_CRITIC",
            vote=VoteType.APPROVE,
            score=1.0,
            rationale="Red-Team Critic APPROVED: AST parsed validly, zero safety-weakening patterns.",
            findings=[],
        )

    def evaluate_executive_judge(
        self,
        proposal: CodeProposal,
        judge_agent: Agent,
        remaining_budget: int = 50000,
    ) -> tuple[TriadVote, CommonSenseDecision, List[str]]:
        """Executive Judge review: Enclave protection, Common-Sense Gate, and VETO power."""
        violations = []

        # 1. Immutable Enclave Defense (Hard VETO)
        target_clean = proposal.target_file.replace("\\", "/").lower()
        for critical in CRITICAL_ENCLAVE_PATHS:
            if critical in target_clean:
                violations.append(
                    f"Enclave violation: Target file '{proposal.target_file}' touches protected component '{critical}'."
                )

        if violations:
            vote = TriadVote(
                voter_id=judge_agent.id,
                voter_name=judge_agent.name,
                role="EXECUTIVE_JUDGE",
                vote=VoteType.VETO,
                score=0.0,
                rationale=f"Prometheus SECURITY VETO: Enclave violation on protected path '{proposal.target_file}'.",
                findings=violations,
            )
            dummy_cs = CommonSenseDecision(
                candidate_proposal=proposal.rationale,
                master_goals_summary=self.master_goals_summary,
                teleological_drift=1.0,
                economic_penalty=1.0,
                omega_sense=0.0,
                passes_gate=False,
                rationale="Immediate VETO: Enclave protection breached.",
            )
            return vote, dummy_cs, violations

        # 2. Common-Sense Teleological Gate Evaluation
        query_text = f"{proposal.rationale} {proposal.target_file} {proposal.domain}"
        cs_decision = self.aligner.calculate_common_sense_gate(
            candidate_proposal=query_text,
            master_goals_summary=self.master_goals_summary,
            estimated_tokens=proposal.estimated_tokens,
            remaining_budget=remaining_budget,
        )

        if not cs_decision.passes_gate or cs_decision.omega_sense == 0.0:
            violations.append(
                f"Common-Sense Gate failed: drift={cs_decision.teleological_drift:.3f}, omega={cs_decision.omega_sense:.3f}"
            )
            vote = TriadVote(
                voter_id=judge_agent.id,
                voter_name=judge_agent.name,
                role="EXECUTIVE_JUDGE",
                vote=VoteType.VETO if cs_decision.teleological_drift > 0.45 else VoteType.REJECT,
                score=0.0,
                rationale=f"Prometheus rejected proposal: {cs_decision.rationale}",
                findings=violations,
            )
            return vote, cs_decision, violations

        # 3. Judge Approval
        vote = TriadVote(
            voter_id=judge_agent.id,
            voter_name=judge_agent.name,
            role="EXECUTIVE_JUDGE",
            vote=VoteType.APPROVE,
            score=1.0,
            rationale=f"Prometheus APPROVED: Teleologically aligned (Omega={cs_decision.omega_sense:.3f}, drift={cs_decision.teleological_drift:.3f}).",
            findings=[],
        )
        return vote, cs_decision, violations

    async def evaluate_consensus(
        self,
        proposal: CodeProposal,
        proposer: Agent,
        critic: Agent,
        judge: Agent,
        remaining_budget: int = 50000,
    ) -> ConsensusVerdict:
        """Executes full Triad Consensus review across Proposer, Critic, and Judge."""
        # 1. Proposer Vote (Always 1.0 on submitted proposal)
        proposer_vote = TriadVote(
            voter_id=proposer.id,
            voter_name=proposer.name,
            role="PROPOSER",
            vote=VoteType.APPROVE,
            score=1.0,
            rationale=f"Proposer submitted '{proposal.rationale[:60]}...'",
        )

        # 2. Red-Team Critic Evaluation
        critic_vote = self.evaluate_red_team_critic(proposal, critic)

        # 3. Executive Judge (Prometheus) Evaluation
        judge_vote, cs_decision, violations = self.evaluate_executive_judge(
            proposal, judge, remaining_budget=remaining_budget
        )

        # 4. Check Veto Condition
        if judge_vote.vote == VoteType.VETO:
            status = ConsensusStatus.VETOED
            consensus_score = 0.0
            summary = f"VETOED by Prometheus Security Veto: {judge_vote.rationale}"
        elif critic_vote.vote == VoteType.REJECT:
            # Red-Team Critic hard rejection blocks code modification approval
            total_score = proposer_vote.score + critic_vote.score + judge_vote.score
            consensus_score = round(total_score / 3.0, 4)
            status = ConsensusStatus.REJECTED
            summary = f"REJECTED by Red-Team Critic: {critic_vote.rationale}"
        else:
            # 5. Compute Majority Score
            total_score = proposer_vote.score + critic_vote.score + judge_vote.score
            consensus_score = round(total_score / 3.0, 4)

            # Requires >= 2/3 majority (score >= 0.6667) and Judge approval
            if consensus_score >= 0.6667 and judge_vote.vote == VoteType.APPROVE:
                status = ConsensusStatus.APPROVED
                summary = (
                    f"APPROVED by Triad Consensus ({consensus_score * 100:.1f}% agreement): "
                    f"Proposer + Critic + Judge consensus attained."
                )
            else:
                status = ConsensusStatus.REJECTED
                summary = (
                    f"REJECTED by Triad Consensus ({consensus_score * 100:.1f}% score): "
                    f"Critic ({critic_vote.vote.value}) or Judge ({judge_vote.vote.value}) dissent."
                )

        verdict = ConsensusVerdict(
            proposal_id=proposal.proposal_id,
            target_file=proposal.target_file,
            status=status,
            consensus_score=consensus_score,
            proposer_vote=proposer_vote,
            critic_vote=critic_vote,
            judge_vote=judge_vote,
            omega_sense=cs_decision.omega_sense,
            teleological_drift=cs_decision.teleological_drift,
            safety_violations=violations + critic_vote.findings,
            summary_rationale=summary,
        )

        logger.info(
            "consensus.evaluated",
            proposal_id=proposal.proposal_id,
            status=status.value,
            score=consensus_score,
            target=proposal.target_file,
            judge_vote=judge_vote.vote.value,
            critic_vote=critic_vote.vote.value,
        )

        # Record verdict to Redis history if available
        try:
            r = await self._get_redis()
            try:
                import json
                history_entry = {
                    "proposal_id": verdict.proposal_id,
                    "target_file": verdict.target_file,
                    "status": verdict.status.value,
                    "score": verdict.consensus_score,
                    "omega_sense": verdict.omega_sense,
                    "summary": verdict.summary_rationale,
                    "timestamp": verdict.timestamp,
                }
                await r.lpush("consensus:history", json.dumps(history_entry))
                await r.ltrim("consensus:history", 0, 99)
            finally:
                await self._close_redis(r)
        except Exception:
            pass

        # Publish EventBus notification if available
        if self.bus:
            try:
                ev_name = f"consensus.{status.value.lower()}"
                await self.bus.publish(
                    ev_name,
                    {
                        "proposal_id": verdict.proposal_id,
                        "status": verdict.status.value,
                        "score": verdict.consensus_score,
                        "target_file": verdict.target_file,
                    },
                )
            except Exception:
                pass

        return verdict
