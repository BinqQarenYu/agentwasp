"""Unit tests for Cognitive Blackboard — System 4 Shared Multi-Agent Deliberation."""

import pytest
from src.agent_manager.blackboard import (
    CognitiveBlackboard,
    Hypothesis,
    PeerCritique,
    StrategicBid,
)


@pytest.mark.asyncio
async def test_blackboard_lifecycle():
    bb = CognitiveBlackboard()

    # 1. Post Hypothesis
    hyp = await bb.post_hypothesis(
        author_bot="bot-09",
        topic="causal_anomaly",
        statement="Observed volatility spike in entity btc_volatility (+24.5%).",
        evidence=["Observed 12 samples over 3h", "Standard deviation > 0.08"],
        confidence=0.75,
    )
    assert hyp.id.startswith("hyp_")
    assert hyp.author_bot == "bot-09"
    assert hyp.status == "PROPOSED"
    assert hyp.confidence == 0.75
    assert len(hyp.evidence) == 2

    # 2. Peer Critique: Affirmation by Athena
    crt1 = await bb.submit_critique(
        hypothesis_id=hyp.id,
        critic_bot="bot-15",
        verdict="AFFIRM",
        counter_argument="Fleet audit verifies related telemetry alerts on upstream connectors.",
        confidence_adjustment=0.10,
    )
    assert crt1.verdict == "AFFIRM"
    assert crt1.confidence_adjustment == 0.10

    # Verify status changed to DEBATING
    h_fetched = await bb.get_hypothesis(hyp.id)
    assert h_fetched is not None
    assert h_fetched.status == "DEBATING"
    assert len(h_fetched.critiques) == 1

    # 3. Agent Capability Bid: Hephaestus bids to execute stabilization
    bid1 = await bb.submit_bid(
        hypothesis_id=hyp.id,
        bot_id="bot-02",
        capability_fit=0.92,
        proposed_strategy="Implement exponential backoff on connector rate limiter.",
        estimated_cost=850,
    )
    assert bid1.bot_id == "bot-02"
    assert bid1.capability_fit == 0.92

    h_with_bid = await bb.get_hypothesis(hyp.id)
    assert len(h_with_bid.bids) == 1

    # 4. Synthesize Consensus
    verdict = await bb.synthesize_consensus(hyp.id)
    assert verdict is not None
    assert verdict["stance"] == "APPROVED_BY_CONSENSUS"
    assert verdict["consensus_score"] == 0.85
    assert verdict["affirms"] == 1

    final_hyp = await bb.get_hypothesis(hyp.id)
    assert final_hyp.status == "SYNTHESIZED"
    assert final_hyp.confidence == 0.85


@pytest.mark.asyncio
async def test_blackboard_redteam_veto():
    bb = CognitiveBlackboard()

    # Proposal with security implications
    hyp = await bb.post_hypothesis(
        author_bot="bot-01",
        topic="security_posture",
        statement="Bypass sandbox inspection for internal webhook payloads to reduce latency.",
        evidence=["Benchmark shows 25ms savings"],
        confidence=0.80,
    )

    # Ares Red-Team submits hard REFUTE
    crt = await bb.submit_critique(
        hypothesis_id=hyp.id,
        critic_bot="bot-10",
        verdict="REFUTE",
        counter_argument="VETO: Invariant violation. Bypassing sandbox exposes arbitrary code execution in enclave.",
        confidence_adjustment=-0.50,
    )
    assert crt.verdict == "REFUTE"

    # Consensus synthesis should reject immediately due to red-team security refute
    verdict = await bb.synthesize_consensus(hyp.id)
    assert verdict["stance"] == "REJECTED_BY_RED_TEAM"
    assert verdict["refutes"] == 1
    assert verdict["consensus_score"] <= 0.35

    final_hyp = await bb.get_hypothesis(hyp.id)
    assert final_hyp.status == "REJECTED"


@pytest.mark.asyncio
async def test_blackboard_filtering():
    bb = CognitiveBlackboard()

    await bb.post_hypothesis("bot-09", "causal_anomaly", "Anomaly A")
    await bb.post_hypothesis("bot-15", "code_hygiene", "Refactor B")
    await bb.post_hypothesis("human-operator", "causal_anomaly", "Anomaly C")

    all_items = await bb.list_hypotheses()
    assert len(all_items) == 3

    anomaly_items = await bb.list_hypotheses(topic="causal_anomaly")
    assert len(anomaly_items) == 2

    hygiene_items = await bb.list_hypotheses(topic="code_hygiene")
    assert len(hygiene_items) == 1
    assert hygiene_items[0].statement == "Refactor B"
