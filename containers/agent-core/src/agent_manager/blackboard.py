"""Cognitive Blackboard — Shared semantic workspace and deliberation engine for the multi-agent mesh.

Enables emergent collective intelligence across the 15-bot workforce and ephemeral subagents:
  1. Hypotheses: Proactive assertions, anomalies, and proposed strategic campaigns.
  2. Peer Critiques: Adversarial debate and empirical verification (Ares, Dr. Albert, Athena).
  3. Strategic Bids: Capability-based task bidding from autonomous agents.
  4. Consensus Synthesis: Epistemic calibration ($C_e$) and Triad agreement scoring.
  5. Operator Steering: Direct human intervention points into shared agent working memory.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import redis.asyncio as aioredis
import structlog

logger = structlog.get_logger()

# Redis Keys
BLACKBOARD_HYPOTHESIS_KEY = "blackboard:hypotheses"
BLACKBOARD_IDS_KEY = "blackboard:ids"
BLACKBOARD_EVENTS_CHANNEL = "events:blackboard"


@dataclass
class PeerCritique:
    """An adversarial critique, empirical verification, or amendment from a peer bot."""
    id: str = field(default_factory=lambda: f"crt_{uuid4().hex[:8]}")
    critic_bot: str = "bot-10"          # Ares (Red-Team), Dr. Albert, Argus, etc.
    verdict: str = "CHALLENGE"          # AFFIRM | CHALLENGE | REFUTE | AMEND
    counter_argument: str = ""
    confidence_adjustment: float = 0.0  # -0.5 to +0.5
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PeerCritique:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class StrategicBid:
    """An agent's capability-fitted bid to own or execute a strategic goal."""
    id: str = field(default_factory=lambda: f"bid_{uuid4().hex[:8]}")
    bot_id: str = ""
    capability_fit: float = 0.8         # 0.0 - 1.0 fit score
    proposed_strategy: str = ""
    estimated_cost: int = 1000          # tokens
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StrategicBid:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Hypothesis:
    """A shared cognitive claim, observation, or campaign proposal."""
    id: str = field(default_factory=lambda: f"hyp_{uuid4().hex[:8]}")
    author_bot: str = "root-liaison"    # Prometheus, Dr. Albert, Athena, human-operator
    topic: str = "general"              # causal_anomaly | code_hygiene | security_posture | strategic_campaign
    statement: str = ""
    evidence: list[str] = field(default_factory=list)
    confidence: float = 0.75            # Epistemic confidence C_e in [0.0, 1.0]
    status: str = "PROPOSED"            # PROPOSED | DEBATING | SYNTHESIZED | RESOLVED | REJECTED
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    critiques: list[dict[str, Any]] = field(default_factory=list)
    bids: list[dict[str, Any]] = field(default_factory=list)
    consensus_verdict: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Hypothesis:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class CognitiveBlackboard:
    """Multi-agent shared cognitive blackboard supporting asynchronous debate,

    bidding, and epistemic consensus synthesis.
    """

    def __init__(self, redis_url: str = "", bus=None):
        self.redis_url = redis_url
        self.bus = bus
        # In-memory storage fallback for offline / mock testing
        self._memory_store: dict[str, Hypothesis] = {}

    async def _get_redis(self):
        if not self.redis_url:
            return None
        try:
            r = aioredis.from_url(self.redis_url, decode_responses=True)
            await r.ping()
            return r
        except Exception:
            return None

    # -----------------------------------------------------------------------
    # Hypothesis Lifecycle
    # -----------------------------------------------------------------------

    async def post_hypothesis(
        self,
        author_bot: str,
        topic: str,
        statement: str,
        evidence: list[str] | None = None,
        confidence: float = 0.75,
        metadata: dict[str, Any] | None = None,
    ) -> Hypothesis:
        """Post a new cognitive hypothesis to the shared blackboard."""
        hyp = Hypothesis(
            author_bot=author_bot,
            topic=topic,
            statement=statement,
            evidence=evidence or [],
            confidence=max(0.0, min(1.0, float(confidence))),
            status="PROPOSED",
            metadata=metadata or {},
        )

        r = await self._get_redis()
        if r:
            try:
                await r.hset(BLACKBOARD_HYPOTHESIS_KEY, hyp.id, json.dumps(hyp.to_dict()))
                await r.lpush(BLACKBOARD_IDS_KEY, hyp.id)
                # Cap active IDs at 500
                await r.ltrim(BLACKBOARD_IDS_KEY, 0, 499)
            finally:
                await r.aclose()
        else:
            self._memory_store[hyp.id] = hyp

        logger.info(
            "blackboard.hypothesis_posted",
            id=hyp.id,
            author=author_bot,
            topic=topic,
            confidence=hyp.confidence,
        )
        return hyp

    async def get_hypothesis(self, hypothesis_id: str) -> Hypothesis | None:
        """Fetch a specific hypothesis by ID."""
        r = await self._get_redis()
        if r:
            try:
                raw = await r.hget(BLACKBOARD_HYPOTHESIS_KEY, hypothesis_id)
                if raw:
                    return Hypothesis.from_dict(json.loads(raw))
            finally:
                await r.aclose()
        return self._memory_store.get(hypothesis_id)

    async def list_hypotheses(
        self,
        status: str | None = None,
        topic: str | None = None,
        limit: int = 50,
    ) -> list[Hypothesis]:
        """List hypotheses with optional status or topic filtering."""
        items: list[Hypothesis] = []
        r = await self._get_redis()
        if r:
            try:
                all_raw = await r.hgetall(BLACKBOARD_HYPOTHESIS_KEY)
                for raw_val in all_raw.values():
                    try:
                        h = Hypothesis.from_dict(json.loads(raw_val))
                        items.append(h)
                    except Exception:
                        continue
            finally:
                await r.aclose()
        else:
            items = list(self._memory_store.values())

        # Sort newest first
        items.sort(key=lambda x: x.created_at, reverse=True)

        if status:
            items = [h for h in items if h.status.upper() == status.upper()]
        if topic:
            items = [h for h in items if h.topic.lower() == topic.lower()]

        return items[:limit]

    # -----------------------------------------------------------------------
    # Peer Critique & Bidding
    # -----------------------------------------------------------------------

    async def submit_critique(
        self,
        hypothesis_id: str,
        critic_bot: str,
        verdict: str,
        counter_argument: str,
        confidence_adjustment: float = 0.0,
    ) -> PeerCritique | None:
        """Submit a peer critique from an agent or human operator."""
        hyp = await self.get_hypothesis(hypothesis_id)
        if not hyp:
            return None

        critique = PeerCritique(
            critic_bot=critic_bot,
            verdict=verdict.upper(),
            counter_argument=counter_argument,
            confidence_adjustment=max(-0.5, min(0.5, float(confidence_adjustment))),
        )

        hyp.critiques.append(critique.to_dict())
        hyp.updated_at = datetime.now(timezone.utc).isoformat()
        if hyp.status == "PROPOSED":
            hyp.status = "DEBATING"

        await self._save_hypothesis(hyp)
        logger.info(
            "blackboard.critique_submitted",
            hyp_id=hypothesis_id,
            critic=critic_bot,
            verdict=critique.verdict,
        )
        return critique

    async def submit_bid(
        self,
        hypothesis_id: str,
        bot_id: str,
        capability_fit: float,
        proposed_strategy: str,
        estimated_cost: int = 1000,
    ) -> StrategicBid | None:
        """Submit an agent's capability bid to execute the hypothesis/campaign."""
        hyp = await self.get_hypothesis(hypothesis_id)
        if not hyp:
            return None

        bid = StrategicBid(
            bot_id=bot_id,
            capability_fit=max(0.0, min(1.0, float(capability_fit))),
            proposed_strategy=proposed_strategy,
            estimated_cost=estimated_cost,
        )

        hyp.bids.append(bid.to_dict())
        hyp.updated_at = datetime.now(timezone.utc).isoformat()
        await self._save_hypothesis(hyp)
        return bid

    # -----------------------------------------------------------------------
    # Consensus Synthesis
    # -----------------------------------------------------------------------

    async def synthesize_consensus(self, hypothesis_id: str) -> dict[str, Any] | None:
        """Synthesize multi-agent critiques and calculate final epistemic consensus."""
        hyp = await self.get_hypothesis(hypothesis_id)
        if not hyp:
            return None

        base_conf = hyp.confidence
        total_adjustment = sum(c.get("confidence_adjustment", 0.0) for c in hyp.critiques)
        
        # Stance counts
        verdicts = [c.get("verdict", "").upper() for c in hyp.critiques]
        affirms = verdicts.count("AFFIRM")
        challenges = verdicts.count("CHALLENGE")
        refutes = verdicts.count("REFUTE")
        amends = verdicts.count("AMEND")

        # Red-Team VETO check: If Ares or Argus REFUTED on security/safety, penalize score
        has_security_refute = any(
            c.get("critic_bot") in ("bot-10", "bot-03") and c.get("verdict") == "REFUTE"
            for c in hyp.critiques
        )

        calibrated_confidence = max(0.0, min(1.0, base_conf + total_adjustment))
        if has_security_refute:
            calibrated_confidence = min(0.35, calibrated_confidence * 0.5)

        # Final stance
        if has_security_refute or (refutes > affirms and calibrated_confidence < 0.40):
            status = "REJECTED"
            verdict_stance = "REJECTED_BY_RED_TEAM"
        elif calibrated_confidence >= 0.65:
            status = "SYNTHESIZED"
            verdict_stance = "APPROVED_BY_CONSENSUS"
        else:
            status = "DEBATING"
            verdict_stance = "REQUIRES_EVIDENCE"

        verdict_data = {
            "consensus_score": round(calibrated_confidence, 4),
            "stance": verdict_stance,
            "affirms": affirms,
            "challenges": challenges,
            "refutes": refutes,
            "amends": amends,
            "synthesized_at": datetime.now(timezone.utc).isoformat(),
        }

        hyp.status = status
        hyp.confidence = round(calibrated_confidence, 4)
        hyp.consensus_verdict = verdict_data
        hyp.updated_at = datetime.now(timezone.utc).isoformat()

        await self._save_hypothesis(hyp)
        logger.info(
            "blackboard.consensus_synthesized",
            id=hyp.id,
            status=status,
            score=calibrated_confidence,
        )
        return verdict_data

    # -----------------------------------------------------------------------
    # Persistence helper
    # -----------------------------------------------------------------------

    async def _save_hypothesis(self, hyp: Hypothesis) -> None:
        r = await self._get_redis()
        if r:
            try:
                await r.hset(BLACKBOARD_HYPOTHESIS_KEY, hyp.id, json.dumps(hyp.to_dict()))
            finally:
                await r.aclose()
        else:
            self._memory_store[hyp.id] = hyp
