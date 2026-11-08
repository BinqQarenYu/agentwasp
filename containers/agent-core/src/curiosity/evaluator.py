"""Curiosity Engine & Mathematical Formulation for Agent WASP.

Implements the Intrinsic Curiosity Index (Ct):
    Ct = H(X) * D_KL(P_prior || P_evidence) * S(X) * (1 - Lambda_t)

Where:
    - H(X): Entropy / knowledge gap (0.0 to 1.0)
    - D_KL: Epistemic surprise / prediction error (0.0 to 1.0)
    - S(X): Domain salience to the operator (0.0 to 1.0)
    - Lambda_t: System load / quota penalty (0.0 = free, 1.0 = throttled)
"""

from __future__ import annotations

import math
import psutil
import structlog
from typing import Dict, List, Optional
from dataclasses import dataclass

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
    "system_integrity": 0.90,       # Health of worker infrastructure, disk, memory
    "documentation": 0.85,          # Keeping clean manuals, handovers, records
    "general_unaligned": 0.15,      # Ambient trivia / unassigned hobbies
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
    is_actionable: bool     # Ct >= threshold


class CuriosityEvaluator:
    """Evaluates intellectual curiosity opportunities and computes Ct."""

    def __init__(
        self,
        curiosity_threshold: float = 0.60,
        ram_throttle_percent: float = 80.0,
        domain_weights: Optional[Dict[str, float]] = None,
    ):
        self.curiosity_threshold = curiosity_threshold
        self.ram_throttle_percent = ram_throttle_percent
        self.domain_weights = domain_weights or DEFAULT_DOMAIN_SALIENCE

    def calculate_system_governor(self) -> float:
        """Compute Lambda_t based on system RAM and load.
        
        Returns a float between 0.0 (healthy) and 1.0 (severely constrained).
        """
        try:
            mem = psutil.virtual_memory()
            if mem.percent >= self.ram_throttle_percent:
                # Scaled penalty up to 1.0
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
        # Clamp inputs between 0.0 and 1.0
        h_x = max(0.0, min(1.0, float(entropy)))
        d_kl = max(0.0, min(1.0, float(surprise)))
        
        salience = custom_salience if custom_salience is not None else self.get_domain_salience(domain)
        salience = max(0.0, min(1.0, float(salience)))

        lambda_t = self.calculate_system_governor()

        # Ct = H(X) * D_KL * S(X) * (1 - Lambda_t)
        # We use a smoothed geometric or product formulation with an epistemic floor
        # To avoid Ct = 0 if surprise is zero (pure curiosity regarding unknown topic H(X)):
        # Effective drive = H(X) * (0.3 + 0.7 * D_KL) * S(X) * (1 - Lambda_t)
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
        )
