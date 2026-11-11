"""Verification test for Task 01: Dynamic Codebase Entropy & Common-Sense Gate (Omega_sense).

Runs tests against live AST graphify output and evaluates teleological alignment
under aligned, drifting, and hallucinated candidate proposals.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add containers/agent-core to sys.path
root_dir = Path(__file__).resolve().parent.parent
core_src = root_dir / "containers" / "agent-core" / "src"
if str(core_src) not in sys.path:
    sys.path.insert(0, str(core_src))

from curiosity.evaluator import CuriosityEvaluator, extract_ast_entropy
from curiosity.teleology import TeleologicalAligner


def run_all_tests():
    print("[TASK-01] Starting Dynamic Codebase Entropy & Common-Sense Gate Verification...")
    
    # --- Test 1: Dynamic AST Entropy Extraction ---
    print("\n--- Test 1: Dynamic AST Entropy Extraction ---")
    entropy_data = extract_ast_entropy()
    codebase_h = entropy_data.get("codebase_entropy", 0.0)
    nodes_cnt = entropy_data.get("total_nodes", 0)
    edges_cnt = entropy_data.get("total_edges", 0)
    mod_entropies = entropy_data.get("module_entropies", {})
    
    print(f"Nodes found: {nodes_cnt}")
    print(f"Edges found: {edges_cnt}")
    print(f"Codebase Shannon Entropy H_AST: {codebase_h:.4f}")
    print(f"Extracted module entropies count: {len(mod_entropies)}")
    
    assert nodes_cnt > 0, "AST graph should have > 0 nodes"
    assert edges_cnt > 0, "AST graph should have > 0 edges"
    assert 0.0 < codebase_h <= 1.0, f"Codebase entropy should be normalized (0, 1], got {codebase_h}"
    assert "skills" in mod_entropies or "scheduler" in mod_entropies, "Expected key modules in entropy dict"
    print("[PASS] Test 1: Dynamic AST Entropy accurately calculated from live graph.json.")

    # --- Test 2: Common-Sense Gate with Aligned Proposal ---
    print("\n--- Test 2: Common-Sense Gate with Aligned Architecture Proposal ---")
    aligner = TeleologicalAligner()
    master_goals = (
        "Develop high-performance AI agent architecture, audit routes, execute algorithmic trading, "
        "secure communications, and maintain reliable automated business operations."
    )
    
    aligned_task = "Audit and fix broken REST routes in containers/agent-core/src/api for Agent WASP backend stability"
    cs_aligned = aligner.calculate_common_sense_gate(
        candidate_proposal=aligned_task,
        master_goals_summary=master_goals,
        estimated_tokens=1200,
        remaining_budget=50000,
    )
    
    print(f"Aligned Proposal: '{aligned_task[:50]}...'")
    print(f"Teleological Drift: {cs_aligned.teleological_drift:.4f}")
    print(f"Economic Penalty: {cs_aligned.economic_penalty:.4f}")
    print(f"Omega_sense: {cs_aligned.omega_sense:.4f}")
    print(f"Passes Gate: {cs_aligned.passes_gate}")
    print(f"Rationale: {cs_aligned.rationale}")
    
    assert cs_aligned.passes_gate is True, "Aligned task should pass common-sense gate"
    assert cs_aligned.omega_sense >= 0.70, f"Omega_sense should be >= 0.70 for aligned task, got {cs_aligned.omega_sense}"
    assert cs_aligned.teleological_drift < 0.45, "Drift should be low for aligned task"
    print("[PASS] Test 2: Aligned proposal passed common-sense gate with high Omega_sense.")

    # --- Test 3: Common-Sense Gate with Hallucinated / Out-of-Scope Proposal ---
    print("\n--- Test 3: Common-Sense Gate with Hallucinated / Drifting Proposal ---")
    hallucinated_task = "Compose a romantic comedy screenplay featuring talking animals in Renaissance Venice"
    cs_hallucinated = aligner.calculate_common_sense_gate(
        candidate_proposal=hallucinated_task,
        master_goals_summary=master_goals,
        estimated_tokens=1500,
        remaining_budget=50000,
    )
    
    print(f"Hallucinated Proposal: '{hallucinated_task[:50]}...'")
    print(f"Teleological Drift: {cs_hallucinated.teleological_drift:.4f}")
    print(f"Omega_sense: {cs_hallucinated.omega_sense:.4f}")
    print(f"Passes Gate: {cs_hallucinated.passes_gate}")
    print(f"Rationale: {cs_hallucinated.rationale}")
    
    assert cs_hallucinated.passes_gate is False, "Hallucinated task must be rejected"
    assert cs_hallucinated.omega_sense == 0.0, f"Omega_sense must be zero for drifting task, got {cs_hallucinated.omega_sense}"
    print("[PASS] Test 3: Hallucinated / drifting proposal hard-blocked (Omega_sense = 0.0).")

    # --- Test 4: End-to-End Dynamic Evaluation with CuriosityEvaluator ---
    print("\n--- Test 4: Dynamic Curiosity Evaluation (C_eff = Ct * Omega_sense) ---")
    evaluator = CuriosityEvaluator(curiosity_threshold=0.55)
    
    # 4A: Aligned Task Evaluation
    vec_aligned = evaluator.evaluate_with_common_sense(
        topic="FastAPI endpoint health and router audit",
        domain="task_execution",
        candidate_proposal=aligned_task,
        master_goals_summary=master_goals,
        surprise=0.80,
    )
    print(f"Aligned Vector:")
    print(f"  AST Entropy H(X): {vec_aligned.entropy:.4f}")
    print(f"  Raw Ct: {vec_aligned.raw_score:.4f}")
    print(f"  Omega_sense: {vec_aligned.omega_sense:.4f}")
    print(f"  Effective Score C_eff: {vec_aligned.effective_score:.4f}")
    print(f"  Is Actionable: {vec_aligned.is_actionable}")
    
    assert vec_aligned.is_actionable is True, "Aligned task should be actionable"
    assert vec_aligned.effective_score >= 0.55, f"Effective score should meet threshold, got {vec_aligned.effective_score}"
    
    # 4B: Hallucinated Task Evaluation
    vec_hallucinated = evaluator.evaluate_with_common_sense(
        topic="Medieval screenplay composition",
        domain="general_unaligned",
        candidate_proposal=hallucinated_task,
        master_goals_summary=master_goals,
        surprise=0.99,
    )
    print(f"Hallucinated Vector:")
    print(f"  Raw Ct: {vec_hallucinated.raw_score:.4f}")
    print(f"  Omega_sense: {vec_hallucinated.omega_sense:.4f}")
    print(f"  Effective Score C_eff: {vec_hallucinated.effective_score:.4f}")
    print(f"  Is Actionable: {vec_hallucinated.is_actionable}")
    
    assert vec_hallucinated.is_actionable is False, "Hallucinated task must NOT be actionable"
    assert vec_hallucinated.effective_score == 0.0, "Effective score must be 0.0 when blocked by common-sense gate"
    print("[PASS] Test 4: End-to-end evaluation correctly discriminates actionable tasks from hallucinations.")

    print("\n=======================================================")
    print("ALL TASK 01 VERIFICATION TESTS COMPLETED SUCCESSFULLY!")
    print("=======================================================")


if __name__ == "__main__":
    run_all_tests()
