"""Unit tests for Strategic Campaign Engine — System 4 Causal Strategic Synthesizer."""

import pytest
from src.agent_manager.blackboard import CognitiveBlackboard
from src.world.strategic_engine import (
    CampaignStage,
    StrategicCampaign,
    StrategicCampaignEngine,
)


@pytest.mark.asyncio
async def test_strategic_campaign_synthesis():
    bb = CognitiveBlackboard()
    engine = StrategicCampaignEngine(blackboard=bb)

    campaign = await engine.synthesize_campaign(
        entity="api_rate_limit_errors",
        trend="volatile",
        change_pct=42.5,
        current_value="18 failures/min",
    )

    assert campaign.id.startswith("cmp_")
    assert campaign.target_entity == "api_rate_limit_errors"
    assert campaign.status == "SYNTHESIZED"
    assert campaign.priority_score >= 0.50
    assert len(campaign.stages) == 3

    # Check stage composition and role assignment
    stage1 = campaign.stages[0]
    stage2 = campaign.stages[1]
    stage3 = campaign.stages[2]

    assert stage1["assigned_bot"] == "bot-09"  # Dr. Albert (Forensics)
    assert stage2["assigned_bot"] == "bot-10"  # Ares (Red-Team Threat)
    assert stage3["assigned_bot"] == "bot-15"  # Athena (Fleet Steward / Stabilization)

    # Check that campaign was automatically posted onto the Cognitive Blackboard
    hypotheses = await bb.list_hypotheses(topic="strategic_campaign")
    assert len(hypotheses) == 1
    hyp = hypotheses[0]
    assert hyp.author_bot == "root-liaison"
    assert "api_rate_limit_errors" in hyp.statement
    assert hyp.confidence == campaign.priority_score

    # Check that Dr. Albert submitted an initial peer critique
    assert len(hyp.critiques) == 1
    assert hyp.critiques[0]["critic_bot"] == "bot-09"
    assert hyp.critiques[0]["verdict"] == "AFFIRM"


@pytest.mark.asyncio
async def test_campaign_listing():
    bb = CognitiveBlackboard()
    engine = StrategicCampaignEngine(blackboard=bb)

    c1 = await engine.synthesize_campaign("metric_disk", "volatile", 22.0)
    c2 = await engine.synthesize_campaign("metric_latency", "up", 35.0)

    campaigns = await engine.list_campaigns(limit=10)
    assert len(campaigns) == 2
    # Sorted by priority_score descending
    assert campaigns[0].priority_score >= campaigns[1].priority_score
