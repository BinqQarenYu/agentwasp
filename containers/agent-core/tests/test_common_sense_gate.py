"""Unit test for Dynamic AST Entropy & Common-Sense Gate (Task 01)."""

import sys
from pathlib import Path

core_src = Path(__file__).resolve().parent.parent / "src"
if str(core_src) not in sys.path:
    sys.path.insert(0, str(core_src))

import unittest
from curiosity.evaluator import CuriosityEvaluator, extract_ast_entropy
from curiosity.teleology import TeleologicalAligner, CommonSenseDecision


class TestCommonSenseGate(unittest.TestCase):
    def setUp(self):
        self.aligner = TeleologicalAligner()
        self.evaluator = CuriosityEvaluator(curiosity_threshold=0.55)
        self.master_goals = (
            "Develop high-performance AI agent architecture, audit routes, execute algorithmic trading, "
            "secure communications, and maintain reliable automated business operations."
        )

    def test_extract_ast_entropy(self):
        entropy_data = extract_ast_entropy()
        self.assertIn("codebase_entropy", entropy_data)
        self.assertGreater(entropy_data["codebase_entropy"], 0.0)
        self.assertLessEqual(entropy_data["codebase_entropy"], 1.0)
        self.assertGreater(entropy_data["total_nodes"], 0)
        self.assertGreater(entropy_data["total_edges"], 0)
        self.assertIsInstance(entropy_data["module_entropies"], dict)

    def test_aligned_candidate_passes(self):
        candidate = "Refactor agent router and audit FastAPI REST endpoints for core stability"
        decision = self.aligner.calculate_common_sense_gate(
            candidate_proposal=candidate,
            master_goals_summary=self.master_goals,
            estimated_tokens=1500,
            remaining_budget=50000,
        )
        self.assertTrue(decision.passes_gate)
        self.assertGreaterEqual(decision.omega_sense, 0.70)
        self.assertLess(decision.teleological_drift, 0.45)

    def test_hallucinated_candidate_rejected(self):
        candidate = "Write high-fantasy fiction stories about knights fighting dragons in dungeons"
        decision = self.aligner.calculate_common_sense_gate(
            candidate_proposal=candidate,
            master_goals_summary=self.master_goals,
            estimated_tokens=1500,
            remaining_budget=50000,
        )
        self.assertFalse(decision.passes_gate)
        self.assertEqual(decision.omega_sense, 0.0)
        self.assertGreater(decision.teleological_drift, 0.45)

    def test_end_to_end_evaluation(self):
        aligned_vec = self.evaluator.evaluate_with_common_sense(
            topic="Audit router endpoints",
            domain="task_execution",
            candidate_proposal="Audit router endpoints for agent architecture",
            master_goals_summary=self.master_goals,
            surprise=0.75,
        )
        self.assertTrue(aligned_vec.is_actionable)
        self.assertGreater(aligned_vec.effective_score, 0.0)

        drift_vec = self.evaluator.evaluate_with_common_sense(
            topic="Dragon fiction",
            domain="general_unaligned",
            candidate_proposal="Write dragon stories for entertainment",
            master_goals_summary=self.master_goals,
        )
        self.assertFalse(drift_vec.is_actionable)
        self.assertEqual(drift_vec.effective_score, 0.0)
        self.assertEqual(drift_vec.omega_sense, 0.0)


if __name__ == "__main__":
    unittest.main()
