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

import ast
import asyncio
import math
import os
import time
from collections import Counter
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

ATSE_ENCLAVE_EXCLUDE = frozenset({
    "sandbox.py",
    "python_exec.py",
    "control_layer.py",
    "redaction.py",
    "self_improve.py",
    "auth.py",
    "auth_routes.py",
})


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
            fallback_idx = min(2, len(curr.parents) - 1)
            self.workspace_root = found or curr.parents[fallback_idx]
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
        self._evaluated_targets: List[str] = []

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

    @staticmethod
    def _file_ast_entropy(py_file: Path) -> float:
        """Compute normalized Shannon entropy of AST node types for a single Python module."""
        try:
            source = py_file.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(source)
        except Exception:
            return 0.0
        counts: Counter[str] = Counter(type(n).__name__ for n in ast.walk(tree))
        total = sum(counts.values())
        if total < 5 or len(counts) < 2:
            return 0.50
        h = -sum((c / total) * math.log2(c / total) for c in counts.values())
        h_max = math.log2(len(counts))
        return round(min(1.0, max(0.0, h / h_max)), 4) if h_max > 0 else 0.50

    def _discover_candidate_files(self, max_files: int = 80) -> List[tuple[str, float]]:
        """Discover eligible workspace .py modules ranked by freshness and AST entropy."""
        src_roots = [
            self.workspace_root / "containers" / "agent-core" / "src",
            self.workspace_root / "src",
        ]
        src_root = next((r for r in src_roots if r.exists() and r.is_dir()), None)
        if src_root is None:
            return []

        discovered: List[tuple[bool, float, str]] = []
        count = 0
        for py_file in sorted(src_root.rglob("*.py")):
            if count >= max_files:
                break
            if "__pycache__" in py_file.parts or "tests" in py_file.parts:
                continue
            if py_file.name == "__init__.py" or py_file.name in ATSE_ENCLAVE_EXCLUDE:
                continue
            try:
                rel_path = py_file.relative_to(self.workspace_root).as_posix()
            except ValueError:
                rel_path = py_file.relative_to(src_root.parent).as_posix()
            ent = self._file_ast_entropy(py_file)
            if ent <= 0.0:
                continue
            count += 1
            is_fresh = rel_path not in self._evaluated_targets
            discovered.append((is_fresh, ent, rel_path))

        if discovered and not any(item[0] for item in discovered):
            self._evaluated_targets.clear()
            discovered = [(True, ent, rel) for _, ent, rel in discovered]

        discovered.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [(rel, ent) for _, ent, rel in discovered]

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
        selected_entropy = codebase_h
        selected_surprise = 0.75

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

            # Dynamically scan and rotate across highest-entropy eligible codebase modules
            ranked_candidates = self._discover_candidate_files()
            for cand_rel, cand_ent in ranked_candidates:
                cand_path = self.workspace_root / cand_rel
                if cand_path.exists() and cand_path.is_file():
                    try:
                        src_text = cand_path.read_text(encoding="utf-8")
                        mut_prop = self.ast_mutator.mutate_source(src_text, cand_rel, strategy="all")
                        if mut_prop.changes_made and mut_prop.mutated_source:
                            target_rel = cand_rel
                            proposed_code = mut_prop.mutated_source
                            rationale = f"AST Code Optimization on {cand_rel}: {mut_prop.rationale}"
                            selected_entropy = max(codebase_h, cand_ent)
                            selected_surprise = round(min(0.95, 0.60 + 0.08 * len(mut_prop.changes_made)), 4)
                            self._evaluated_targets.append(cand_rel)
                            if len(self._evaluated_targets) > 30:
                                self._evaluated_targets = self._evaluated_targets[-30:]
                            break
                    except Exception as e:
                        logger.warning("atse.ast_mutator_skipped", target=cand_rel, error=str(e)[:80])

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
            entropy=selected_entropy,
            surprise=selected_surprise,
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
