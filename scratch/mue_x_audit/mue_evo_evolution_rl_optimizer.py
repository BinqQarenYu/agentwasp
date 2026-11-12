"""RL Optimizer — Reinforcement learning on mutation outcomes.

Every mutation is a trial. The scorer tracks which genes succeed.
This optimizer uses that feedback to select the best mutation strategies,
prioritizing approaches that produce real improvements and avoiding
patterns that cause failures.

Strategy weights are continuously updated via:
- Success → reinforce (increase weight)
- Failure → penalize (decrease weight, record anti-pattern)
- Stagnation → explore (try alternate strategies)
"""

import random
import time
from dataclasses import dataclass, field


STRATEGIES = ["repair", "optimize", "explore", "exploit", "innovate", "prune"]


@dataclass
class StrategyWeight:
    strategy: str
    weight: float = 1.0
    successes: int = 0
    failures: int = 0
    total_impact: float = 0.0
    last_used: float = 0.0

    @property
    def success_rate(self) -> float:
        total = self.successes + self.failures
        return self.successes / max(total, 1)

    @property
    def effective_weight(self) -> float:
        """Weight modulated by success rate and recency."""
        recency_bonus = 1.0
        if self.last_used > 0:
            hours_since = (time.time() - self.last_used) / 3600
            recency_bonus = max(0.5, 1.0 - hours_since * 0.1)
        return self.weight * (0.3 + 0.7 * self.success_rate) * recency_bonus


class RLOptimizer:
    """Reinforcement learning for mutation strategy selection.

    Uses the TaskScorer feedback loop to learn which strategies
    produce the best results for different gene types and contexts.
    """

    def __init__(self, scorer=None):
        self.scorer = scorer
        self.strategies: dict[str, StrategyWeight] = {
            s: StrategyWeight(strategy=s) for s in STRATEGIES
        }
        self.gene_strategy_history: dict[str, list[dict]] = {}
        self.total_trials = 0
        self.successful_trials = 0

    def select_strategy(self, gene_name: str = None, context: dict = None) -> str:
        """Select the best mutation strategy based on learned weights.

        Uses epsilon-greedy: 80% exploit (best known), 20% explore (random).
        With scorer feedback, biases toward strategies that improved similar genes.
        """
        context = context or {}

        # If scorer available, get reinforcement signals
        if self.scorer and gene_name:
            signal = self.scorer.get_reinforcement_signal(gene_name)
            if signal["action"] == "revert_pattern":
                # Gene is degrading — prioritize repair
                return "repair"
            if signal["action"] == "reinforce" and signal["score"] > 0.7:
                # Gene is strong — try exploit or innovate
                return random.choice(["exploit", "innovate"])

        # Epsilon-greedy strategy selection
        if random.random() < 0.2:
            # Explore: pick random strategy
            return random.choice(STRATEGIES)

        # Exploit: weighted random from strategy weights
        weights = [self.strategies[s].effective_weight for s in STRATEGIES]
        total = sum(weights)
        if total == 0:
            return random.choice(STRATEGIES)

        r = random.random() * total
        cumulative = 0
        for i, s in enumerate(STRATEGIES):
            cumulative += weights[i]
            if r <= cumulative:
                return s
        return STRATEGIES[-1]

    def select_target_gene(self, genome) -> str | None:
        """Select which gene to mutate based on performance feedback.

        Weak genes get mutated to improve. Strong genes get reinforced.
        Unknown genes get explored.
        """
        if not self.scorer or not genome or not genome.genes:
            if genome and genome.genes:
                return random.choice(list(genome.genes.keys()))
            return None

        gene_names = list(genome.genes.keys())
        if not gene_names:
            return None

        # Get weakest and strongest from scorer
        weakest = self.scorer.get_weakest_genes(3)
        strongest = self.scorer.get_strongest_genes(3)

        # 50% target weakest (improve), 30% target strongest (reinforce), 20% random
        roll = random.random()
        candidates = [g for g in weakest if g in gene_names]
        if roll < 0.5 and candidates:
            return random.choice(candidates)
        candidates = [g for g in strongest if g in gene_names]
        if roll < 0.8 and candidates:
            return random.choice(candidates)
        return random.choice(gene_names)

    def record_outcome(self, strategy: str, gene_name: str, success: bool, impact: float = 0.5):
        """Record a mutation outcome to update strategy weights."""
        if strategy not in self.strategies:
            return

        sw = self.strategies[strategy]
        sw.last_used = time.time()
        self.total_trials += 1

        if success:
            sw.successes += 1
            sw.total_impact += impact
            sw.weight *= 1.05  # Reinforce: +5%
            sw.weight = min(5.0, sw.weight)
            self.successful_trials += 1
        else:
            sw.failures += 1
            sw.weight /= 1.05  # Symmetric penalize: 1/1.05 = 0.952 → product = 1.0 (stable equilibrium)
            sw.weight = max(0.1, sw.weight)

        # Track per-gene history
        if gene_name not in self.gene_strategy_history:
            self.gene_strategy_history[gene_name] = []
        self.gene_strategy_history[gene_name].append({
            "strategy": strategy,
            "success": success,
            "impact": impact,
            "time": time.time(),
        })
        # Keep last 50 per gene
        if len(self.gene_strategy_history[gene_name]) > 50:
            self.gene_strategy_history[gene_name] = \
                self.gene_strategy_history[gene_name][-50:]

    def get_best_strategies(self, n: int = 3) -> list[str]:
        """Return the top N strategies by effective weight."""
        ranked = sorted(self.strategies.values(),
                       key=lambda s: s.effective_weight, reverse=True)
        return [s.strategy for s in ranked[:n]]

    def get_gene_strategy_stats(self, gene_name: str) -> dict:
        """Get strategy performance stats for a specific gene."""
        history = self.gene_strategy_history.get(gene_name, [])
        if not history:
            return {"gene": gene_name, "trials": 0}

        by_strategy = {}
        for entry in history:
            s = entry["strategy"]
            if s not in by_strategy:
                by_strategy[s] = {"successes": 0, "failures": 0}
            if entry["success"]:
                by_strategy[s]["successes"] += 1
            else:
                by_strategy[s]["failures"] += 1

        return {
            "gene": gene_name,
            "trials": len(history),
            "by_strategy": by_strategy,
            "best_strategy": max(by_strategy,
                key=lambda s: by_strategy[s]["successes"] / max(
                    by_strategy[s]["successes"] + by_strategy[s]["failures"], 1
                )
            ) if by_strategy else None,
        }

    @property
    def stats(self) -> dict:
        return {
            "total_trials": self.total_trials,
            "success_rate": self.successful_trials / max(self.total_trials, 1),
            "strategies": {
                s: {
                    "weight": round(sw.effective_weight, 3),
                    "success_rate": round(sw.success_rate, 3),
                    "trials": sw.successes + sw.failures,
                }
                for s, sw in self.strategies.items()
            },
            "genes_tracked": len(self.gene_strategy_history),
        }
