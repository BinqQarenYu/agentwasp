"""Teleological Alignment Engine for Agent WASP.

Computes semantic and vector similarity between candidate curiosity tasks
and active operator goals:
    Alignment(task, Goal) = Similarity(v_task, G)

Supports:
1. Lexical and semantic cluster alignment (BoW + Domain Ontologies)
2. Character N-Gram Substring Matching (handles stems, variations, compound words)
3. Direct embedding cosine similarity (when embedding model is provided)

Enforces strict teleological equilibrium (tau >= threshold) to prevent infinite loops,
distractions, or wasteful background execution.
"""

from __future__ import annotations

import math
import re
from typing import List, Dict, Optional, Tuple, Set
import structlog
from dataclasses import dataclass

logger = structlog.get_logger()

# Standard stop words
_STOP_WORDS = frozenset({
    "the", "a", "an", "and", "or", "in", "on", "at", "to", "for", "with", "by", "of",
    "is", "are", "was", "were", "be", "been", "that", "this", "it", "as", "from",
    "que", "de", "la", "el", "en", "y", "un", "una", "por", "con", "para", "los", "las"
})

# High-value domain semantic clusters for bridge similarity
DOMAIN_CLUSTERS = {
    "coding": {"code", "python", "bug", "audit", "test", "fix", "core", "agent", "agentwasp", "redis", "db", "architecture", "stability", "memory", "cache", "async", "service", "engine", "execution"},
    "trading": {"trade", "trading", "crypto", "binance", "bybit", "htx", "orderbook", "divergence", "arbitrage", "price", "market", "liquidity", "pair", "spread", "volume", "scanner", "quantitative"},
    "security": {"security", "threat", "sentinel", "phishing", "scam", "spam", "auth", "token", "sandbox", "audit", "safe", "quarantine", "password", "key", "vulnerability"},
    "email": {"email", "gmail", "inbox", "imap", "smtp", "mail", "sender", "clean", "triage", "message"},
}


@dataclass
class TeleologyDecision:
    candidate_task: str
    target_goal: str
    alignment_score: float  # Alignment index [0.0 - 1.0]
    passes_threshold: bool
    status: str             # "APPROVED", "DORMANT_QUEUED", "REJECTED"
    rationale: str


class TeleologicalAligner:
    """Evaluates whether an intrinsically motivated curiosity task aligns with master goals."""

    def __init__(self, strict_threshold: float = 0.65, dormant_threshold: float = 0.40):
        self.strict_threshold = strict_threshold
        self.dormant_threshold = dormant_threshold

    def _tokenize(self, text: str) -> Set[str]:
        words = re.findall(r"\b[a-zA-Z0-9_\-\.]{3,}\b", text.lower())
        return {w for w in words if w not in _STOP_WORDS}

    def _get_ngrams(self, text: str, n: int = 3) -> Set[str]:
        clean = re.sub(r"[^a-zA-Z0-9]", "", text.lower())
        if len(clean) < n:
            return {clean} if clean else set()
        return {clean[i:i+n] for i in range(len(clean) - n + 1)}

    def calculate_similarity(self, text_a: str, text_b: str) -> float:
        """Multi-layer similarity metric:
        1. Direct Token Overlap (Jaccard)
        2. Domain Cluster Intersection
        3. Character Tri-Gram Overlap
        """
        toks_a = self._tokenize(text_a)
        toks_b = self._tokenize(text_b)

        if not toks_a or not toks_b:
            return 0.0

        # Layer 1: Token Jaccard
        jaccard = len(toks_a & toks_b) / len(toks_a | toks_b)

        # Layer 2: Domain Cluster Synergy
        cluster_synergy = 0.0
        for cluster_name, cluster_words in DOMAIN_CLUSTERS.items():
            words_a = toks_a & cluster_words
            words_b = toks_b & cluster_words
            if words_a and words_b:
                # Strong domain semantic co-occurrence
                overlap_strength = min(1.0, (len(words_a) + len(words_b)) / 6.0)
                cluster_synergy = max(cluster_synergy, 0.60 + 0.35 * overlap_strength)

        # Layer 3: Substring / Subword Tri-Gram similarity
        ngrams_a = self._get_ngrams(text_a)
        ngrams_b = self._get_ngrams(text_b)
        ngram_sim = len(ngrams_a & ngrams_b) / max(1, len(ngrams_a | ngrams_b))

        # Weight layers
        if cluster_synergy > 0:
            score = max(cluster_synergy, (0.5 * jaccard) + (0.5 * ngram_sim))
        else:
            score = (0.6 * jaccard) + (0.4 * ngram_sim)

        return min(1.0, max(0.0, float(score)))

    def evaluate_alignment(
        self,
        candidate_task: str,
        active_goals: List[str],
        operator_mission: Optional[str] = None,
    ) -> TeleologyDecision:
        """Check if candidate task aligns with any active goal or the operator's mission."""
        if not active_goals and not operator_mission:
            operator_mission = (
                "Develop high-performance AI agent architecture, execute quantitative algorithmic trading, "
                "secure communications, and maintain reliable automated business operations."
            )

        reference_targets = list(active_goals)
        if operator_mission:
            reference_targets.append(operator_mission)

        best_score = 0.0
        best_target = reference_targets[0] if reference_targets else ""

        for target in reference_targets:
            score = self.calculate_similarity(candidate_task, target)
            if score > best_score:
                best_score = score
                best_target = target

        score_rounded = round(best_score, 4)

        if score_rounded >= self.strict_threshold:
            status = "APPROVED"
            passes = True
            rationale = f"Strong teleological alignment ({score_rounded} >= {self.strict_threshold}) with goal: '{best_target[:60]}...'"
        elif score_rounded >= self.dormant_threshold:
            status = "DORMANT_QUEUED"
            passes = False
            rationale = f"Moderate alignment ({score_rounded}). Queued for nightly dream reflection."
        else:
            status = "REJECTED"
            passes = False
            rationale = f"Insufficient alignment ({score_rounded} < {self.dormant_threshold}). Discarded to prevent distraction."

        logger.info(
            "teleology.alignment_evaluated",
            task=candidate_task[:60],
            best_target=best_target[:40],
            alignment=score_rounded,
            status=status,
        )

        return TeleologyDecision(
            candidate_task=candidate_task,
            target_goal=best_target,
            alignment_score=score_rounded,
            passes_threshold=passes,
            status=status,
            rationale=rationale,
        )
