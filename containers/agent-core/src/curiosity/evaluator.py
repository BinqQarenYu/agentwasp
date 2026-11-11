"""Curiosity Engine & Mathematical Formulation for Agent WASP.

Implements the Intrinsic Curiosity Index (Ct):
    Ct = H(X) * D_KL(P_prior || P_evidence) * S(X) * (1 - Lambda_t)

Where:
    - H(X): Dynamic AST Graph Entropy / knowledge gap (0.0 to 1.0)
    - D_KL: Epistemic surprise / prediction error (0.0 to 1.0)
    - S(X): Domain salience to the operator (0.0 to 1.0)
    - Lambda_t: System load / quota penalty (0.0 = free, 1.0 = throttled)

Extended with Common-Sense Teleological Gating (Task 01):
    C_eff = Ct * Omega_sense
    Omega_sense = max(0.0, 1.0 - 2.5 * (Delta_drift ** 2)) * (1.0 - TokenCost / TokenBudget)
"""

from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
import psutil
import structlog

try:
    from .teleology import TeleologicalAligner, CommonSenseDecision
except ImportError:
    from teleology import TeleologicalAligner, CommonSenseDecision

logger = structlog.get_logger()

# Default Salience weights for Operator domains
DEFAULT_DOMAIN_SALIENCE: Dict[str, float] = {
    # Autonomous Employee / Operational Worker Priorities
    "task_execution": 1.0,          # Doing work delegated by operator
    "workflow_automation": 1.0,     # Optimizing standard operating procedures (SOPs)
    "operations_management": 0.95,  # Tracking deliverables, pipelines, schedules
    "communication_inbox": 0.95,    # Triaging client/team communications, emails
    "quality_assurance": 0.95,      # Auditing work accuracy, catching errors before operator sees them
    "tool_competence": 0.90,        # Mastering APIs, browser scripts, local environment
    "coding": 0.90,                 # Codebase maintenance, refactoring, bug fixing
    "codebase_evolution": 0.95,     # Autonomous teleological code evolution (ATSE)
    "system_integrity": 0.90,       # Health of worker infrastructure, disk, memory
    "documentation": 0.85,          # Keeping clean manuals, handovers, records
    "general_unaligned": 0.15,      # Ambient trivia / unassigned hobbies
}


def extract_ast_entropy(graph_json_path: str = "") -> Dict[str, Any]:
    """Calculates Normalized Degree Shannon Entropy H_AST from the AST graphify output.

    Returns:
        Dict with keys:
            - "codebase_entropy": float (0.0 to 1.0)
            - "total_nodes": int
            - "total_edges": int
            - "module_entropies": Dict[str, float]
    """
    candidates: List[Path] = []
    if graph_json_path:
        candidates.append(Path(graph_json_path))

    this_dir = Path(__file__).resolve().parent
    candidates.extend([
        this_dir.parent / "graphify-out" / "graph.json",
        Path("containers/agent-core/src/graphify-out/graph.json"),
        Path("src/graphify-out/graph.json"),
        Path("graphify-out/graph.json"),
    ])

    resolved_path: Optional[Path] = None
    for cand in candidates:
        if cand.exists():
            resolved_path = cand
            break

    if not resolved_path:
        logger.warning("curiosity.ast_graph_not_found", tried=[str(c) for c in candidates])
        return {
            "codebase_entropy": 0.50,
            "total_nodes": 0,
            "total_edges": 0,
            "module_entropies": {},
        }

    try:
        with open(resolved_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        nodes = data.get("nodes", [])
        edges = data.get("edges", [])

        if not nodes or not edges:
            return {
                "codebase_entropy": 0.50,
                "total_nodes": len(nodes),
                "total_edges": len(edges),
                "module_entropies": {},
            }

        degree: Dict[str, int] = defaultdict(int)
        node_module: Dict[str, str] = {}
        module_degrees: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))

        for n in nodes:
            nid = n.get("id", "")
            path_str = n.get("path", nid).replace("\\", "/")
            parts = [p for p in path_str.split("/") if p and p != "."]
            mod = parts[0] if parts else "root"
            node_module[nid] = mod

        for e in edges:
            s = e.get("source", "")
            t = e.get("target", "")
            degree[s] += 1
            degree[t] += 1
            mod_s = node_module.get(s, "external")
            mod_t = node_module.get(t, "external")
            module_degrees[mod_s][s] += 1
            module_degrees[mod_t][t] += 1

        total_deg = sum(degree.values())
        if total_deg > 0 and len(degree) > 1:
            h = -sum((cnt / total_deg) * math.log2(cnt / total_deg) for cnt in degree.values() if cnt > 0)
            h_max = math.log2(len(degree))
            h_norm = min(1.0, max(0.0, h / h_max))
        else:
            h_norm = 0.50

        mod_entropies: Dict[str, float] = {}
        for mod, mod_deg_map in module_degrees.items():
            tot = sum(mod_deg_map.values())
            if tot > 0 and len(mod_deg_map) > 1:
                mod_h = -sum((cnt / tot) * math.log2(cnt / tot) for cnt in mod_deg_map.values() if cnt > 0)
                mod_h_max = math.log2(len(mod_deg_map))
                mod_entropies[mod] = round(min(1.0, max(0.0, mod_h / mod_h_max)), 4)
            else:
                mod_entropies[mod] = 0.50

        return {
            "codebase_entropy": round(h_norm, 4),
            "total_nodes": len(nodes),
            "total_edges": len(edges),
            "module_entropies": mod_entropies,
        }
    except Exception as exc:
        logger.error("curiosity.extract_ast_entropy_failed", error=str(exc))
        return {
            "codebase_entropy": 0.50,
            "total_nodes": 0,
            "total_edges": 0,
            "module_entropies": {},
        }


@dataclass
class CuriosityVector:
    topic: str
    domain: str
    entropy: float          # H(X)
    surprise: float         # D_KL
    salience: float         # S(X)
    governor_penalty: float # Lambda_t
    raw_score: float        # Ct
    is_actionable: bool = False     # Actionable after common-sense gate
    omega_sense: float = 1.0       # Common-sense gate factor [0.0, 1.0]
    effective_score: float = 0.0   # C_eff = Ct * omega_sense
    delta_drift: float = 0.0       # Teleological drift
    common_sense_rationale: str = ""


class CuriosityEvaluator:
    """Evaluates intellectual curiosity opportunities and computes Ct."""

    def __init__(
        self,
        curiosity_threshold: float = 0.60,
        ram_throttle_percent: float = 80.0,
        domain_weights: Optional[Dict[str, float]] = None,
        aligner: Optional[TeleologicalAligner] = None,
    ):
        self.curiosity_threshold = curiosity_threshold
        self.ram_throttle_percent = ram_throttle_percent
        self.domain_weights = domain_weights or DEFAULT_DOMAIN_SALIENCE
        self.aligner = aligner or TeleologicalAligner()
        self._ast_entropy_cache: Optional[Dict[str, Any]] = None

    def calculate_system_governor(self) -> float:
        """Compute Lambda_t based on system RAM and load.

        Returns a float between 0.0 (healthy) and 1.0 (severely constrained).
        """
        try:
            mem = psutil.virtual_memory()
            if mem.percent >= self.ram_throttle_percent:
                excess = (mem.percent - self.ram_throttle_percent) / (100.0 - self.ram_throttle_percent)
                return min(1.0, max(0.0, 0.5 + 0.5 * excess))
            return 0.0
        except Exception:
            return 0.0

    def get_domain_salience(self, domain: str) -> float:
        """Retrieve operator salience weight for the given domain."""
        domain_clean = domain.lower().strip()
        for key, val in self.domain_weights.items():
            if key in domain_clean or domain_clean in key:
                return val
        return self.domain_weights.get("general", 0.20)

    def evaluate(
        self,
        topic: str,
        domain: str,
        entropy: float,
        surprise: float,
        custom_salience: Optional[float] = None,
    ) -> CuriosityVector:
        """Compute Intrinsic Curiosity Index Ct for an event or topic."""
        h_x = max(0.0, min(1.0, float(entropy)))
        d_kl = max(0.0, min(1.0, float(surprise)))

        salience = custom_salience if custom_salience is not None else self.get_domain_salience(domain)
        salience = max(0.0, min(1.0, float(salience)))

        lambda_t = self.calculate_system_governor()

        effective_curiosity = h_x * (0.35 + 0.65 * d_kl) * salience * (1.0 - lambda_t)
        raw_score = round(effective_curiosity, 4)
        is_actionable = raw_score >= self.curiosity_threshold

        logger.debug(
            "curiosity.evaluated",
            topic=topic,
            domain=domain,
            Ct=raw_score,
            entropy=h_x,
            surprise=d_kl,
            salience=salience,
            governor=lambda_t,
            actionable=is_actionable,
        )

        return CuriosityVector(
            topic=topic,
            domain=domain,
            entropy=h_x,
            surprise=d_kl,
            salience=salience,
            governor_penalty=lambda_t,
            raw_score=raw_score,
            is_actionable=is_actionable,
            omega_sense=1.0,
            effective_score=raw_score,
            delta_drift=0.0,
            common_sense_rationale="Evaluated without common-sense filter",
        )

    def evaluate_with_common_sense(
        self,
        topic: str,
        domain: str,
        candidate_proposal: str,
        master_goals_summary: str,
        entropy: Optional[float] = None,
        surprise: float = 0.5,
        custom_salience: Optional[float] = None,
        estimated_tokens: int = 1500,
        remaining_budget: int = 50000,
        ast_graph_path: str = "",
    ) -> CuriosityVector:
        """Evaluates curiosity grounded in codebase AST entropy and gated by common sense.

        Mathematical Formulation:
            H(X) = extracted AST entropy (module or codebase)
            Ct = H(X) * (0.35 + 0.65 * D_KL) * S(X) * (1 - Lambda_t)
            Omega_sense = CommonSenseGate(candidate_proposal, master_goals_summary)
            C_eff = Ct * Omega_sense
            Actionable = (C_eff >= curiosity_threshold) AND (Omega_sense > 0.0)
        """
        # Resolve dynamic AST entropy if not explicitly provided
        if entropy is None:
            if self._ast_entropy_cache is None:
                self._ast_entropy_cache = extract_ast_entropy(ast_graph_path)

            # Match domain or topic against module entropies
            mod_entropies = self._ast_entropy_cache.get("module_entropies", {})
            matched_entropy = None
            query_str = f"{domain} {topic}".lower()
            for mod_name, mod_h in mod_entropies.items():
                if mod_name.lower() in query_str:
                    matched_entropy = mod_h
                    break

            entropy = matched_entropy if matched_entropy is not None else self._ast_entropy_cache.get("codebase_entropy", 0.70)

        # Compute intrinsic curiosity index Ct
        raw_vec = self.evaluate(
            topic=topic,
            domain=domain,
            entropy=entropy,
            surprise=surprise,
            custom_salience=custom_salience,
        )

        # Pass through Common-Sense Gate
        cs_decision = self.aligner.calculate_common_sense_gate(
            candidate_proposal=candidate_proposal,
            master_goals_summary=master_goals_summary,
            estimated_tokens=estimated_tokens,
            remaining_budget=remaining_budget,
        )

        # Compute effective bounded curiosity
        effective_score = round(raw_vec.raw_score * cs_decision.omega_sense, 4)
        is_actionable = (effective_score >= self.curiosity_threshold and cs_decision.passes_gate)

        logger.info(
            "curiosity.common_sense_evaluation_complete",
            topic=topic[:40],
            raw_Ct=raw_vec.raw_score,
            omega_sense=cs_decision.omega_sense,
            effective_score=effective_score,
            actionable=is_actionable,
            passes_gate=cs_decision.passes_gate,
        )

        return CuriosityVector(
            topic=topic,
            domain=domain,
            entropy=raw_vec.entropy,
            surprise=raw_vec.surprise,
            salience=raw_vec.salience,
            governor_penalty=raw_vec.governor_penalty,
            raw_score=raw_vec.raw_score,
            omega_sense=cs_decision.omega_sense,
            effective_score=effective_score,
            delta_drift=cs_decision.teleological_drift,
            common_sense_rationale=cs_decision.rationale,
            is_actionable=is_actionable,
        )
