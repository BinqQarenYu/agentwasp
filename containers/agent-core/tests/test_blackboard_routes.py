"""Unit tests for Cognitive Blackboard and Causal Campaign dashboard routes."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

core_dir = Path(__file__).resolve().parent.parent
if str(core_dir) not in sys.path:
    sys.path.insert(0, str(core_dir))

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.templating import Jinja2Templates

from src.dashboard.routes.blackboard import router as blackboard_router, get_blackboard


class MockInMemoryRedis:
    def __init__(self):
        self.hashes = {}
        self.lists = {}

    async def ping(self):
        return True

    async def hset(self, key, field, value):
        if key not in self.hashes:
            self.hashes[key] = {}
        self.hashes[key][field] = value

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    async def hgetall(self, key):
        return self.hashes.get(key, {})

    async def lpush(self, key, value):
        if key not in self.lists:
            self.lists[key] = []
        self.lists[key].insert(0, value)

    async def ltrim(self, key, start, stop):
        if key in self.lists:
            self.lists[key] = self.lists[key][start : stop + 1]

    async def aclose(self):
        pass


@pytest.fixture
def client():
    app = FastAPI()
    templates_dir = core_dir / "src" / "dashboard" / "templates"
    app.state.templates = Jinja2Templates(directory=str(templates_dir))
    app.state.redis_url = ""
    app.include_router(blackboard_router, prefix="/blackboard")

    # Reset blackboard state
    bb = get_blackboard()
    bb._memory_store.clear()

    return TestClient(app)


def test_blackboard_html_page(client):
    """Test that GET /blackboard renders successfully with seed data."""
    resp = client.get("/blackboard")
    assert resp.status_code == 200
    assert "Cognitive Blackboard" in resp.text
    assert "Causal Stabilization Campaign" in resp.text or "Active Hypotheses" in resp.text


def test_api_post_and_list_hypotheses(client):
    """Test POST /blackboard/api/hypotheses and GET /blackboard/api/hypotheses."""
    # 1. Post hypothesis
    payload = {
        "author_bot": "bot-09",
        "topic": "causal_anomaly",
        "statement": "Anomalous error rate spike detected in mail triage.",
        "evidence": ["Error rate 4.2%", "Benchmark baseline 0.1%"],
        "confidence": 0.88,
    }
    resp = client.post("/blackboard/api/hypotheses", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    hyp = data["hypothesis"]
    assert hyp["id"].startswith("hyp_")
    assert hyp["confidence"] == 0.88

    # 2. List hypotheses
    resp_list = client.get("/blackboard/api/hypotheses")
    assert resp_list.status_code == 200
    list_data = resp_list.json()
    assert list_data["ok"] is True
    assert list_data["count"] >= 1


def test_api_peer_critique_and_consensus(client):
    """Test submitting peer critiques and calculating epistemic consensus."""
    # Post hypothesis
    h_resp = client.post("/blackboard/api/hypotheses", json={
        "author_bot": "bot-15",
        "topic": "code_hygiene",
        "statement": "Refactor scheduler jobs to use cached monotonic clock.",
        "confidence": 0.70,
    })
    hyp_id = h_resp.json()["hypothesis"]["id"]

    # Submit critique by Ares
    c_resp = client.post(f"/blackboard/api/hypotheses/{hyp_id}/critique", json={
        "critic_bot": "bot-10",
        "verdict": "CHALLENGE",
        "counter_argument": "Requires testing across container time drift in Docker.",
        "confidence_adjustment": -0.05,
    })
    assert c_resp.status_code == 200
    assert c_resp.json()["ok"] is True

    # Submit critique by Dr. Albert
    c2_resp = client.post(f"/blackboard/api/hypotheses/{hyp_id}/critique", json={
        "critic_bot": "bot-09",
        "verdict": "AFFIRM",
        "counter_argument": "Benchmark confirms 12% lower context switch overhead.",
        "confidence_adjustment": 0.15,
    })
    assert c2_resp.status_code == 200

    # Submit bid by Hephaestus
    b_resp = client.post(f"/blackboard/api/hypotheses/{hyp_id}/bid", json={
        "bot_id": "bot-02",
        "capability_fit": 0.95,
        "proposed_strategy": "Patch clock import with shadow staging validation.",
        "estimated_cost": 500,
    })
    assert b_resp.status_code == 200
    assert b_resp.json()["ok"] is True

    # Synthesize consensus
    s_resp = client.post(f"/blackboard/api/hypotheses/{hyp_id}/synthesize")
    assert s_resp.status_code == 200
    s_data = s_resp.json()
    assert s_data["ok"] is True
    assert s_data["verdict"]["stance"] == "APPROVED_BY_CONSENSUS"
    assert s_data["verdict"]["consensus_score"] == 0.80


def test_api_campaign_synthesis(client):
    """Test POST /blackboard/api/campaigns/synthesize and GET /blackboard/api/campaigns."""
    resp = client.post("/blackboard/api/campaigns/synthesize", json={
        "entity": "github_fleet_commit_velocity",
        "trend": "down",
        "change_pct": -28.4,
        "current_value": "12 commits/week",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    cmp = data["campaign"]
    assert cmp["target_entity"] == "github_fleet_commit_velocity"
    assert cmp["priority_score"] >= 0.50
    assert len(cmp["stages"]) == 3

    # Check list campaigns
    list_resp = client.get("/blackboard/api/campaigns")
    assert list_resp.status_code == 200
    assert list_resp.json()["count"] >= 1
