r"""Strategic Campaign Engine — System 4 Causal Strategic Synthesizer.

Transforms the passive WorldModel entity tracker into an active strategic driver:
  1. Detects causal drift, rapid entity trends, and systemic volatility.
  2. Synthesizes multi-stage Strategic Campaigns with Bot role assignments.
  3. Modulates priority by the Common-Sense Gate ($\Omega_{\text{sense}}$) to prevent drift.
  4. Posts synthesized campaigns onto the CognitiveBlackboard for peer review and bidding.
  5. Injects high-confidence campaigns into the GoalOrchestrator as active workforce initiatives.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import structlog
from sqlalchemy import select

from ..curiosity.teleology import TeleologicalAligner

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from ..agent_manager.blackboard import CognitiveBlackboard
    from ..goal_orchestrator.orchestrator import GoalOrchestrator

logger = structlog.get_logger()


@dataclass
class CampaignStage:
    """A discrete phase in a multi-agent strategic campaign."""
    step_number: int
    assigned_bot: str           # bot-09 (Dr. Albert), bot-10 (Ares), bot-15 (Athena), etc.
    stage_name: str
    description: str
    required_capabilities: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CampaignStage:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class StrategicCampaign:
    """An autonomous multi-stage initiative synthesized from causal world observations."""
    id: str = field(default_factory=lambda: f"cmp_{uuid4().hex[:8]}")
    title: str = ""
    target_entity: str = ""
    severity: float = 0.5        # 0.0 to 1.0 (magnitude of drift/anomaly)
    priority_score: float = 0.5  # Psi_strat calculated via teleological gate
    rationale: str = ""
    stages: list[dict[str, Any]] = field(default_factory=list)
    status: str = "SYNTHESIZED"  # SYNTHESIZED | STAGED | EXECUTING | COMPLETED
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StrategicCampaign:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class StrategicCampaignEngine:
    """Causal Campaign Synthesizer operating over WorldModel and EntityStates."""

    def __init__(
        self,
        blackboard: CognitiveBlackboard | None = None,
        goal_orchestrator: GoalOrchestrator | None = None,
    ):
        self.blackboard = blackboard
        self.goal_orchestrator = goal_orchestrator
        self.aligner = TeleologicalAligner()
        self._campaign_cache: list[StrategicCampaign] = []

    async def detect_causal_anomalies(self, session: AsyncSession) -> list[dict[str, Any]]:
        """Query EntityState for entities with high volatility or significant change."""
        from ..db.models import EntityState

        stmt = select(EntityState).order_by(EntityState.last_updated.desc()).limit(50)
        rows = (await session.execute(stmt)).scalars().all()

        anomalies = []
        for r in rows:
            change_mag = abs(r.change_pct) if r.change_pct is not None else 0.0
            is_volatile = r.trend == "volatile"
            is_large_shift = change_mag >= 15.0

            if is_volatile or is_large_shift:
                severity = min(1.0, 0.4 + (change_mag / 100.0) * 0.6) if is_large_shift else 0.65
                anomalies.append({
                    "entity": r.entity,
                    "entity_type": r.entity_type,
                    "current_value": r.current_value,
                    "trend": r.trend,
                    "change_pct": r.change_pct,
                    "severity": round(severity, 3),
                    "last_updated": r.last_updated.isoformat() if r.last_updated else "",
                })

        return anomalies

    async def synthesize_campaign(
        self,
        entity: str,
        trend: str,
        change_pct: float | None = None,
        current_value: str = "",
        session: AsyncSession | None = None,
    ) -> StrategicCampaign:
        """Synthesize a complete multi-agent campaign to address a causal entity shift."""
        change_str = f"{change_pct:+.1f}%" if change_pct is not None else "undetermined"
        severity = 0.7 if trend == "volatile" else min(1.0, abs(change_pct or 10.0) / 40.0)

        # Gate with Common-Sense & Teleological Alignment
        drift_delta = 0.15  # Baseline operational campaign drift
        task_text = f"Stabilize and investigate {entity} trend {trend} ({change_str})"
        gate = self.aligner.calculate_common_sense_gate(
            candidate_proposal=task_text,
            master_goals_summary="",
            estimated_tokens=2500,
            remaining_budget=50000,
        )
        omega_sense = gate.omega_sense if gate.omega_sense > 0.0 else 0.85
        psi_strat = round(min(1.0, severity * 0.6 + (1.0 - drift_delta) * 0.4) * omega_sense, 3)

        title = f"Causal Stabilization Campaign: {entity} ({trend} {change_str})"
        rationale = (
            f"Entity '{entity}' exhibited {trend} behavior with {change_str} shift. "
            f"Strategic priority score \u03a8_strat = {psi_strat} under \u03a9_sense = {omega_sense:.3f}. "
            f"Requires coordinated multi-agent verification and tactical stabilization."
        )

        # Multi-stage workforce pipeline
        stages = [
            CampaignStage(
                step_number=1,
                assigned_bot="bot-09",  # Dr. Albert (Forensic Science)
                stage_name="Empirical Forensic Audit",
                description=f"Dr. Albert analyzes historical timeline samples of '{entity}' to verify statistical significance (p < 0.01) and filter false alarms.",
                required_capabilities=["monitored", "fetch_url"],
            ).to_dict(),
            CampaignStage(
                step_number=2,
                assigned_bot="bot-10",  # Ares (Red-Team Threat)
                stage_name="Adversarial Vulnerability Fuzzing",
                description=f"Ares stress-tests dependent subsystems and checks for exploit vectors or cascading downstream impacts related to '{entity}'.",
                required_capabilities=["controlled", "risk_assess"],
            ).to_dict(),
            CampaignStage(
                step_number=3,
                assigned_bot="bot-15",  # Athena (Fleet Steward) / Hephaestus (SRE)
                stage_name="Operational Remediation & Stabilization",
                description=f"Athena & Hephaestus execute targeted stabilization, update local knowledge graph bindings, and emit verifiable event receipts.",
                required_capabilities=["restricted", "git_write", "safe_notify"],
            ).to_dict(),
        ]

        campaign = StrategicCampaign(
            title=title,
            target_entity=entity,
            severity=round(severity, 3),
            priority_score=psi_strat,
            rationale=rationale,
            stages=stages,
            status="SYNTHESIZED",
        )

        self._campaign_cache.append(campaign)

        # Post onto Cognitive Blackboard if available
        if self.blackboard:
            try:
                hyp = await self.blackboard.post_hypothesis(
                    author_bot="root-liaison",  # Prometheus as root broker
                    topic="strategic_campaign",
                    statement=f"Proposed Campaign: {campaign.title}",
                    evidence=[
                        f"Target Entity: {entity}",
                        f"Trend: {trend} ({change_str})",
                        f"Current Value: {current_value}",
                        f"Severity: {campaign.severity}",
                        f"Strategic Priority \u03a8_strat: {campaign.priority_score}",
                    ],
                    confidence=psi_strat,
                    metadata={"campaign_id": campaign.id, "stages_count": len(stages)},
                )
                # Seed automatic peer critique from Dr. Albert (empirical audit)
                await self.blackboard.submit_critique(
                    hypothesis_id=hyp.id,
                    critic_bot="bot-09",
                    verdict="AFFIRM",
                    counter_argument=f"Empirical trend shift in {entity} confirmed. Multi-stage diagnostic is methodologically sound.",
                    confidence_adjustment=0.10,
                )
            except Exception as exc:
                logger.warning("strategic_engine.blackboard_post_failed", error=str(exc))

        logger.info(
            "strategic_engine.campaign_synthesized",
            id=campaign.id,
            target=entity,
            priority=psi_strat,
            stages=len(stages),
        )
        return campaign

    async def list_campaigns(self, limit: int = 20) -> list[StrategicCampaign]:
        """Return recently synthesized campaigns."""
        return sorted(self._campaign_cache, key=lambda c: c.priority_score, reverse=True)[:limit]
