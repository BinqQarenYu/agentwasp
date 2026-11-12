"""AutonomicEvolutionJob — 24/7 Autonomous Teleological Self-Evolution (ATSE) Loop for Agent WASP.

Orchestrates the complete continuous closed loop:
1. Ingest AST Codebase Topology & Normalized Entropy H_AST (Task 01).
2. Formulate intrinsically motivated candidate self-improvement proposal.
3. Pass through the Common-Sense Gate (Omega_sense) to ensure zero teleological drift (Task 01).
4. Spawn a bounded Depth-1 ephemeral subagent (e.g. Code Auditor / Optimizer) (Task 02).
5. Submit candidate diff to the Triad Adversarial Consensus Protocol (Task 03):
   - Proposer (Athena / Subagent)
   - Red-Team Critic (Ares)
   - Executive Judge (Prometheus) with Security VETO
6. Stage in Shadow Staging Worktree and verify via AST & py_compile (Task 04).
7. Atomically promote to production with instant rollback safety (<500ms SLA).
8. Cleanly reap the ephemeral subagent and emit real-time event telemetry.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

import structlog

try:
    from ..agent_manager.consensus import (
        CodeProposal,
        ConsensusStatus,
        TriadConsensusEngine,
        VoteType,
    )
    from ..agent_manager.genesis import SubagentGenesis
    from ..agent_manager.types import Agent, AgentStatus
    from ..curiosity.ast_mutator import ASTMutator
    from ..curiosity.evaluator import CuriosityEvaluator, extract_ast_entropy
    from ..curiosity.teleology import TeleologicalAligner
    from ..utils.shadow_staging import ShadowStagingManager, StagingState
except (ImportError, ValueError):
    try:
        from src.agent_manager.consensus import (
            CodeProposal,
            ConsensusStatus,
            TriadConsensusEngine,
            VoteType,
        )
        from src.agent_manager.genesis import SubagentGenesis
        from src.agent_manager.types import Agent, AgentStatus
        from src.curiosity.ast_mutator import ASTMutator
        from src.curiosity.evaluator import CuriosityEvaluator, extract_ast_entropy
        from src.curiosity.teleology import TeleologicalAligner
        from src.utils.shadow_staging import ShadowStagingManager, StagingState
    except (ImportError, ValueError):
        from agent_manager.consensus import (
            CodeProposal,
            ConsensusStatus,
            TriadConsensusEngine,
            VoteType,
        )
        from agent_manager.genesis import SubagentGenesis
        from agent_manager.types import Agent, AgentStatus
        from curiosity.ast_mutator import ASTMutator
        from curiosity.evaluator import CuriosityEvaluator, extract_ast_entropy
        from curiosity.teleology import TeleologicalAligner
        from utils.shadow_staging import ShadowStagingManager, StagingState

logger = structlog.get_logger()


class AutonomicEvolutionJob:
    """Recurring 24/7 background job driving autonomous teleological self-evolution."""

    def __init__(
        self,
        workspace_root: Optional[Path] = None,
        redis_url: str = "redis://localhost:6379/0",
        bus=None,
        redis_client=None,
        curiosity_threshold: float = 0.55,
        master_goals_summary: str = "",
        dry_run: bool = False,
    ):
        if workspace_root:
            self.workspace_root = Path(workspace_root)
        else:
            curr = Path(__file__).resolve()
            found = None
            for pr in curr.parents:
                if (pr / ".git").exists() or (pr / "MEMORY.md").exists() or (pr / "run_dashboard_native.py").exists():
                    found = pr
                    break
            self.workspace_root = found or curr.parents[4]
        self.redis_url = redis_url
        self.bus = bus
        self._custom_redis = redis_client
        self.curiosity_threshold = curiosity_threshold
        self.dry_run = dry_run

        self.aligner = TeleologicalAligner()
        self.evaluator = CuriosityEvaluator(
            curiosity_threshold=self.curiosity_threshold,
            aligner=self.aligner,
        )
        self.consensus_engine = TriadConsensusEngine(
            teleological_aligner=self.aligner,
            master_goals_summary=master_goals_summary,
            redis_url=self.redis_url,
            bus=self.bus,
            redis_client=self._custom_redis,
        )
        self.genesis = SubagentGenesis(
            redis_url=self.redis_url,
            bus=self.bus,
            redis_client=self._custom_redis,
        )
        self.staging_manager = ShadowStagingManager(workspace_root=self.workspace_root)
        self.ast_mutator = ASTMutator(workspace_root=self.workspace_root)

        # Default Triad Agents
        self.prometheus = Agent(
            name="root-liaison",
            description="Prometheus - Executive Judge & Enclave Sentinel",
            depth=0,
            max_subagents=3,
        )
        self.athena = Agent(
            name="bot-15",
            description="Athena - Architecture & Code Steward",
            depth=0,
            max_subagents=3,
        )
        self.ares = Agent(
            name="bot-10",
            description="Ares - Adversarial Red-Team Critic",
            depth=0,
        )

    async def execute_cycle(
        self,
        candidate_proposal: Optional[CodeProposal] = None,
        remaining_budget: int = 50000,
    ) -> Dict[str, Any]:
        """Executes one full ATSE cycle."""
        t0 = time.monotonic()
        cycle_id = f"atse_{uuid4().hex[:8]}"
        logger.info("atse.cycle_started", cycle_id=cycle_id)

        # Step 1: Ingest AST Codebase Topology & Entropy
        entropy_data = extract_ast_entropy()
        codebase_h = entropy_data.get("codebase_entropy", 0.70)
        total_nodes = entropy_data.get("total_nodes", 0)
        total_edges = entropy_data.get("total_edges", 0)

        # Step 2: Formulate Candidate Proposal if not supplied
        if candidate_proposal is None:
            proposal_id = f"prop_{uuid4().hex[:6]}"
            target_rel = "containers/agent-core/src/dashboard/routes/telemetry_atse.py"
            proposed_code = (
                "# Autonomously Generated Telemetry Module (ATSE)\n"
                "import time\n\n"
                "def get_atse_health():\n"
                "    \"\"\"Return live ATSE subsystem health metric.\"\"\"\n"
                "    return {'status': 'healthy', 'timestamp': int(time.time()), 'autonomous': True}\n"
            )
            rationale = "Add autonomous telemetry health metric endpoint for router observability"

            # Check if candidate route files can be optimized via AST mutation
            candidate_files = [
                "containers/agent-core/src/dashboard/routes/metrics.py",
                "containers/agent-core/src/dashboard/routes/telemetry_atse.py",
            ]
            for cand_rel in candidate_files:
                cand_path = self.workspace_root / cand_rel
                if cand_path.exists() and cand_path.is_file():
                    try:
                        src_text = cand_path.read_text(encoding="utf-8")
                        mut_prop = self.ast_mutator.mutate_source(src_text, cand_rel, strategy="all")
                        if mut_prop.changes_made and mut_prop.mutated_source:
                            target_rel = cand_rel
                            proposed_code = mut_prop.mutated_source
                            rationale = f"AST Code Optimization: {mut_prop.rationale}"
                            break
                    except Exception as e:
                        logger.warning("atse.ast_mutator_skipped", error=str(e)[:80])

            candidate_proposal = CodeProposal(
                proposal_id=proposal_id,
                target_file=target_rel,
                proposed_code=proposed_code,
                rationale=rationale,
                proposer_agent_id=self.athena.id,
                proposer_name=self.athena.name,
                domain="task_execution",
                estimated_tokens=1200,
            )

        # Step 3: Evaluate with Common-Sense Gate (Omega_sense)
        curiosity_vec = self.evaluator.evaluate_with_common_sense(
            topic=candidate_proposal.rationale,
            domain=candidate_proposal.domain,
            candidate_proposal=f"{candidate_proposal.rationale} {candidate_proposal.target_file}",
            master_goals_summary=self.consensus_engine.master_goals_summary,
            entropy=codebase_h,
            surprise=0.75,
            estimated_tokens=candidate_proposal.estimated_tokens,
            remaining_budget=remaining_budget,
        )

        if not curiosity_vec.is_actionable or curiosity_vec.omega_sense == 0.0:
            latency_ms = round((time.monotonic() - t0) * 1000)
            logger.warning(
                "atse.proposal_suppressed_by_gate",
                proposal_id=candidate_proposal.proposal_id,
                omega_sense=curiosity_vec.omega_sense,
                effective_score=curiosity_vec.effective_score,
            )
            return {
                "cycle_id": cycle_id,
                "status": "SUPPRESSED_BY_GATE",
                "omega_sense": curiosity_vec.omega_sense,
                "effective_score": curiosity_vec.effective_score,
                "proposal_id": candidate_proposal.proposal_id,
                "rationale": curiosity_vec.common_sense_rationale,
                "latency_ms": latency_ms,
            }

        # Step 4: Ephemeral Subagent Genesis (Depth-1)
        subagent = None
        try:
            subagent = await self.genesis.spawn_subagent(
                parent=self.prometheus,
                role="ATSE Code Optimizer",
                task_objective=f"Draft and optimize patch for {candidate_proposal.target_file}",
                skills_needed=["read_file", "system_probe"],
                timeout_seconds=120,
            )

            # Step 5: Triad Adversarial Consensus Evaluation
            verdict = await self.consensus_engine.evaluate_consensus(
                proposal=candidate_proposal,
                proposer=self.athena,
                critic=self.ares,
                judge=self.prometheus,
                remaining_budget=remaining_budget,
            )

            if not verdict.is_approved:
                latency_ms = round((time.monotonic() - t0) * 1000)
                logger.info(
                    "atse.proposal_consensus_rejected",
                    proposal_id=candidate_proposal.proposal_id,
                    verdict=verdict.status.value,
                    score=verdict.consensus_score,
                )
                return {
                    "cycle_id": cycle_id,
                    "status": f"CONSENSUS_{verdict.status.value}",
                    "verdict": verdict.status.value,
                    "consensus_score": verdict.consensus_score,
                    "proposal_id": candidate_proposal.proposal_id,
                    "rationale": verdict.summary_rationale,
                    "safety_violations": verdict.safety_violations,
                    "latency_ms": latency_ms,
                }

            # Step 6: Shadow Staging & Pre-Flight Verification
            if self.dry_run:
                latency_ms = round((time.monotonic() - t0) * 1000)
                return {
                    "cycle_id": cycle_id,
                    "status": "APPROVED_DRY_RUN",
                    "proposal_id": candidate_proposal.proposal_id,
                    "target_file": candidate_proposal.target_file,
                    "consensus_score": verdict.consensus_score,
                    "omega_sense": verdict.omega_sense,
                    "latency_ms": latency_ms,
                }

            session = self.staging_manager.create_staging_session(
                proposal_id=candidate_proposal.proposal_id,
                target_relative_path=candidate_proposal.target_file,
                proposed_content=candidate_proposal.proposed_code,
            )

            verif = self.staging_manager.verify_staging(session)
            if not verif.is_valid:
                self.staging_manager.rollback_staged_code(session, reason="Pre-flight verification failed")
                latency_ms = round((time.monotonic() - t0) * 1000)
                return {
                    "cycle_id": cycle_id,
                    "status": "VERIFICATION_FAILED_ROLLEDBACK",
                    "proposal_id": candidate_proposal.proposal_id,
                    "errors": verif.errors,
                    "latency_ms": latency_ms,
                }

            # Step 7: Atomic Promotion
            promoted = self.staging_manager.promote_staged_code(session)
            latency_ms = round((time.monotonic() - t0) * 1000)

            if promoted:
                logger.info(
                    "atse.cycle_completed_successfully",
                    cycle_id=cycle_id,
                    target_file=candidate_proposal.target_file,
                    latency_ms=latency_ms,
                )
                return {
                    "cycle_id": cycle_id,
                    "status": "PROMOTED_SUCCESSFULLY",
                    "target_file": candidate_proposal.target_file,
                    "proposal_id": candidate_proposal.proposal_id,
                    "consensus_score": verdict.consensus_score,
                    "omega_sense": verdict.omega_sense,
                    "codebase_entropy": codebase_h,
                    "total_nodes": total_nodes,
                    "latency_ms": latency_ms,
                }
            else:
                return {
                    "cycle_id": cycle_id,
                    "status": "PROMOTION_FAILED_ROLLEDBACK",
                    "proposal_id": candidate_proposal.proposal_id,
                    "latency_ms": latency_ms,
                }

        finally:
            # Step 8: Clean Reaping of Ephemeral Subagent
            if subagent:
                await self.genesis.reap_subagent(
                    parent=self.prometheus,
                    subagent_id=subagent.id,
                    reason="atse_cycle_complete",
                )

    async def __call__(self) -> str:
        """Invoked by the background scheduler runner."""
        try:
            res = await self.execute_cycle()
            status = res.get("status", "UNKNOWN")
            latency = res.get("latency_ms", 0)
            return f"ATSE Cycle {res.get('cycle_id', '')} - {status} ({latency}ms)"
        except Exception as exc:
            logger.exception("atse.job_execution_failed", error=str(exc))
            return f"ATSE Cycle error: {str(exc)[:100]}"
