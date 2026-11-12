"""Unit Tests for Hardened AST Code Mutation Engine (Task 09 - MUE-X Integration)."""

import ast
import unittest
from pathlib import Path

from src.curiosity.ast_mutator import (
    ASTMutator,
    OptimizationInjector,
    ExploitInjector,
    ResilienceInjector,
    _VariableRenamer,
)


class TestASTMutator(unittest.TestCase):
    """Test suite verifying AST mutation safety, correctness, and deduplication."""

    def test_variable_renamer_avoids_substring_corruption(self):
        """Verify that variable renamer renames only exact Name nodes, avoiding matrix -> matri_x."""
        code = "def process(x, matrix):\n    return matrix + x"
        tree = ast.parse(code)
        renamer = _VariableRenamer("x", "_x")
        new_tree = renamer.visit(tree)
        new_code = ast.unparse(new_tree)
        self.assertIn("return matrix + _x", new_code)
        self.assertNotIn("matri_x", new_code)

    def test_optimization_constant_folding(self):
        """Verify constant string folding optimizes concatenated literals."""
        code = 'header = "agent" + "_" + "wasp" + "_v2"'
        tree = ast.parse(code)
        opt = OptimizationInjector()
        new_tree = opt.visit(tree)
        self.assertTrue(opt.changes_made)
        new_code = ast.unparse(new_tree)
        self.assertIn("header = 'agent_wasp_v2'", new_code)

    def test_optimization_loop_to_comprehension(self):
        """Verify for-loop append is transformed into list comprehension."""
        code = (
            "results = []\n"
            "for item in raw_items:\n"
            "    results.append(item * 2)\n"
        )
        tree = ast.parse(code)
        opt = OptimizationInjector()
        new_tree = opt.visit(tree)
        self.assertTrue(opt.changes_made)
        new_code = ast.unparse(new_tree)
        self.assertIn("results.extend([_item * 2 for _item in raw_items])", new_code)

    def test_optimization_lru_cache_on_pure_function(self):
        """Verify deterministic pure function receives @functools.lru_cache."""
        code = (
            "def calculate_entropy(prob: float) -> float:\n"
            '    """Deterministic pure entropy calculation."""\n'
            "    return prob * 0.5\n"
        )
        tree = ast.parse(code)
        opt = OptimizationInjector()
        new_tree = opt.visit(tree)
        self.assertTrue(opt.changes_made)
        new_code = ast.unparse(new_tree)
        self.assertIn("@functools.lru_cache(128)", new_code)

    def test_exploit_synthesizes_repr_for_annotated_class(self):
        """Verify classes with type annotations without __repr__ receive a synthesized __repr__."""
        code = (
            "class AgentTelemetry:\n"
            "    agent_id: str\n"
            "    score: float\n"
            "    is_active: bool\n"
        )
        tree = ast.parse(code)
        exp = ExploitInjector()
        new_tree = exp.visit(tree)
        self.assertTrue(exp.changes_made)
        new_code = ast.unparse(new_tree)
        self.assertIn("def __repr__(self):", new_code)
        self.assertIn("AgentTelemetry(agent_id={self.agent_id!r}", new_code)

    def test_resilience_injects_safe_divide(self):
        """Verify resilience injector safely appends safe division primitive."""
        code = "def compute_ratio(a, b):\n    return a / b\n"
        res = ResilienceInjector.inject_pattern(code, "safe_divide")
        self.assertIsNotNone(res)
        self.assertIn("def _safe_divide(a: float, b: float", res)
        # Verify it parses cleanly
        self.assertIsNotNone(ast.parse(res))

    def test_ast_mutator_generates_diff_and_deduplicates(self):
        """Verify ASTMutator produces valid unified diff and rejects duplicate hashes."""
        mutator = ASTMutator()
        source = (
            "class RouteMetric:\n"
            "    path: str\n"
            "    latency: float\n"
        )
        prop = mutator.mutate_source(source, "src/metrics.py", strategy="exploit")
        self.assertTrue(prop.changes_made)
        self.assertIn("@@", prop.diff)
        self.assertIn("+    def __repr__(self):", prop.diff)

        # Re-applying same proposal should be rejected by deduplication
        prop_dup = mutator.mutate_source(source, "src/metrics.py", strategy="exploit")
        self.assertFalse(prop_dup.changes_made)
        self.assertIn("already generated", prop_dup.rationale)

    def test_ast_mutator_gracefully_handles_syntax_errors(self):
        """Verify ASTMutator does not raise on broken syntax."""
        mutator = ASTMutator()
        broken = "def broken(:"
        prop = mutator.mutate_source(broken, "src/broken.py")
        self.assertFalse(prop.changes_made)
        self.assertEqual(prop.mutation_type, "error")


if __name__ == "__main__":
    unittest.main()
