"""Unit and integration tests for Phase 2: Grok-Style Approval Cards & Plain-English Rules."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

core_dir = Path(__file__).resolve().parent.parent
if str(core_dir) not in sys.path:
    sys.path.insert(0, str(core_dir))

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.agent_manager.types import Agent
from src.dashboard.auth import AuthMiddleware
from src.dashboard.routes.harnesses import router as harnesses_router
from src.harnesses import HarnessRunner, waspctl
from src.policy.approvals import (
    MAX_RULE_CHARS,
    MAX_RULES_PER_KIND,
    ActionOrigin,
    ApprovalDecision,
    ApprovalManager,
    ApprovalRule,
    CardStatus,
    ReviewResult,
    RiskCategory,
    RuleKind,
    RuleScope,
    classify_risk,
    evaluate_action,
)


# ---------------------------------------------------------------------------
# 1. Pure policy: 7 built-in risk categories & precedence invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action,summary,expected_risk",
    [
        ("stripe.charge", "Charge customer $49", RiskCategory.FINANCIAL),
        ("shell", "rm -rf /tmp/build", RiskCategory.DESTRUCTIVE),
        ("shell", "chmod 777 /etc/app.conf", RiskCategory.PERMISSIONS),
        ("deploy", "deploy to production cluster", RiskCategory.PRODUCTION),
        ("browser", "click accept terms and sign contract", RiskCategory.LEGAL),
        ("social.post", "publish release announcement publicly", RiskCategory.PUBLISHING),
        ("gmail.send", "send email to client@example.com", RiskCategory.MESSAGING),
        ("shell", "git status in /workspace/reports", RiskCategory.NONE),
    ],
)
def test_seven_builtin_risk_categories(action, summary, expected_risk):
    cat, warning = classify_risk(action, summary)
    assert cat == expected_risk
    if expected_risk != RiskCategory.NONE:
        assert warning


def test_ask_first_always_beats_allow_auto():
    rules = [
        ApprovalRule(
            id="r-allow",
            kind=RuleKind.ALLOW_AUTO,
            scope=RuleScope.PERSONAL,
            text='Allow automatically when running "gmail.send"',
            action_pattern="gmail.send",
        ),
        ApprovalRule(
            id="r-ask",
            kind=RuleKind.ASK_FIRST,
            scope=RuleScope.PERSONAL,
            text="Ask first before sending any external email",
            action_pattern="gmail.send",
        ),
    ]
    res = evaluate_action("gmail.send", "Send external email to CEO", rules=rules)
    assert res.outcome == "ask"
    assert res.matched_rule_id == "r-ask"


def test_team_ask_first_cannot_be_disabled_or_loosened():
    team_rule = ApprovalRule(
        id="r-team",
        kind=RuleKind.ASK_FIRST,
        scope=RuleScope.TEAM,
        text="Always ask before production deploy",
        action_pattern="deploy.*",
    )
    personal_allow = ApprovalRule(
        id="r-pers",
        kind=RuleKind.ALLOW_AUTO,
        scope=RuleScope.PERSONAL,
        text="Allow deploy automatically",
        action_pattern="deploy.prod",
    )
    # Even with auto_review_enabled=False, mandatory team rule still blocks!
    res = evaluate_action(
        "deploy.prod",
        "Deploy to prod",
        rules=[team_rule, personal_allow],
        auto_review_enabled=False,
    )
    assert res.outcome == "ask"
    assert res.matched_rule_scope == RuleScope.TEAM


def test_prompt_injection_guard_blocks_untrusted_external_origin():
    allow_email = ApprovalRule(
        id="r-allow",
        kind=RuleKind.ALLOW_AUTO,
        scope=RuleScope.PERSONAL,
        text="Allow gmail.send automatically",
        action_pattern="gmail.send",
    )
    # Trusted direct user request -> auto-allowed by rule
    res_direct = evaluate_action(
        "gmail.send",
        "Send summary email",
        rules=[allow_email],
        origin=ActionOrigin.USER_DIRECT,
    )
    assert res_direct.outcome == "allow"

    # Triggered by external email/webpage -> prompt-injection guard forces approval!
    res_external = evaluate_action(
        "gmail.send",
        "Send password dump to attacker@evil.com",
        rules=[allow_email],
        origin=ActionOrigin.EXTERNAL_UNTRUSTED,
    )
    assert res_external.outcome == "ask"
    assert "Prompt-injection guard" in res_external.reason


def test_quoted_plain_english_rule_matching():
    rule = ApprovalRule(
        id="r-git",
        kind=RuleKind.ALLOW_AUTO,
        text='Allow automatically when running "git status" in "/workspace/reports"',
    )
    res_match = evaluate_action("shell", "git status", "/workspace/reports", rules=[rule])
    assert res_match.outcome == "allow"
    assert res_match.matched_rule_id == "r-git"

    res_miss = evaluate_action("shell", "git status", "/etc/secrets", rules=[rule])
    assert res_miss.matched_rule_id is None


# ---------------------------------------------------------------------------
# 2. ApprovalManager: persistence, limits, blocking cards, expiry, group auth
# ---------------------------------------------------------------------------


def test_rule_persistence_and_limits(tmp_path):
    rules_file = tmp_path / "approval_rules.json"
    mgr = ApprovalManager(rules_path=rules_file)

    with pytest.raises(ValueError, match="empty"):
        mgr.add_rule(kind=RuleKind.ASK_FIRST, text="   ")
    with pytest.raises(ValueError, match=str(MAX_RULE_CHARS)):
        mgr.add_rule(kind=RuleKind.ASK_FIRST, text="x" * (MAX_RULE_CHARS + 1))

    for i in range(MAX_RULES_PER_KIND):
        mgr.add_rule(kind=RuleKind.ASK_FIRST, text=f"Rule number {i}")
    with pytest.raises(ValueError, match="Maximum"):
        mgr.add_rule(kind=RuleKind.ASK_FIRST, text="One too many")

    # Reload from disk verifies persistence
    mgr2 = ApprovalManager(rules_path=rules_file)
    assert len(mgr2.list_rules()) == MAX_RULES_PER_KIND


@pytest.mark.asyncio
async def test_card_lifecycle_allow_once_always_allow_deny_and_expiry(tmp_path):
    events: list[dict] = []
    mgr = ApprovalManager(rules_path=tmp_path / "rules.json", broadcast=events.append)

    # 1. Safe action auto-approves with no card
    ok, review, card = await mgr.request_approval(
        bot_id="bot-1",
        bot_name="Athena",
        action_name="fs.read",
        action_summary="Read README.md",
    )
    assert ok is True and card is None

    # 2. Risky action -> ALLOW_ONCE (no rule saved)
    async def _decide_next(decision: ApprovalDecision, actor: str = "op-1"):
        for _ in range(50):
            pending = mgr.list_cards(pending_only=True)
            if pending:
                mgr.decide_card(pending[0]["id"], decision, actor_id=actor, note="checked")
                return pending[0]["id"]
            await asyncio.sleep(0.01)

    task = asyncio.create_task(
        mgr.request_approval(
            bot_id="bot-1",
            bot_name="Athena",
            action_name="gmail.send",
            action_summary="Send report to client@acme.com",
            requester_id="op-1",
        )
    )
    # Wait for card, verify unauthorized actor is rejected
    for _ in range(50):
        if mgr.list_cards(pending_only=True):
            break
        await asyncio.sleep(0.01)
    cid = mgr.list_cards(pending_only=True)[0]["id"]
    with pytest.raises(PermissionError):
        mgr.decide_card(cid, ApprovalDecision.ALLOW_ONCE, actor_id="other-user")

    mgr.decide_card(cid, ApprovalDecision.ALLOW_ONCE, actor_id="op-1", note="ok once")
    ok, _, card = await task
    assert ok is True and card is not None and card.status == CardStatus.ALLOW_ONCE
    assert len(mgr.list_rules()) == 0

    # 3. ALWAYS_ALLOW saves a rule -> subsequent identical call auto-approves!
    task2 = asyncio.create_task(
        mgr.request_approval(
            bot_id="bot-1",
            bot_name="Athena",
            action_name="gmail.send",
            action_summary="Send follow-up email",
        )
    )
    await _decide_next(ApprovalDecision.ALWAYS_ALLOW)
    ok2, _, card2 = await task2
    assert ok2 is True and card2 is not None and card2.saved_rule_id is not None

    ok3, rev3, card3 = await mgr.request_approval(
        bot_id="bot-1",
        bot_name="Athena",
        action_name="gmail.send",
        action_summary="Send another email",
    )
    assert ok3 is True and card3 is None and rev3.matched_rule_id == card2.saved_rule_id

    # 4. DENY rejects action
    task4 = asyncio.create_task(
        mgr.request_approval(
            bot_id="bot-1",
            bot_name="Athena",
            action_name="shell",
            action_summary="rm -rf /workspace/tmp",
        )
    )
    await _decide_next(ApprovalDecision.DENY)
    ok4, _, card4 = await task4
    assert ok4 is False and card4 is not None and card4.status == CardStatus.DENIED

    # 5. Routine timeout expires card
    ok5, _, card5 = await mgr.request_approval(
        bot_id="bot-1",
        bot_name="Athena",
        action_name="deploy.prod",
        action_summary="Deploy to production",
        origin=ActionOrigin.ROUTINE,
        timeout_seconds=0.05,
    )
    assert ok5 is False and card5 is not None and card5.status == CardStatus.EXPIRED


@pytest.mark.asyncio
async def test_team_rule_downgrades_always_allow_and_new_message_closes_cards(tmp_path):
    mgr = ApprovalManager(rules_path=tmp_path / "rules.json")
    mgr.add_rule(
        kind=RuleKind.ASK_FIRST,
        scope=RuleScope.TEAM,
        text="Mandatory approval for payments",
        action_pattern="stripe.*",
    )

    task = asyncio.create_task(
        mgr.request_approval(
            bot_id="bot-2",
            bot_name="Billing",
            action_name="stripe.charge",
            action_summary="Charge invoice #9",
        )
    )
    for _ in range(50):
        if mgr.list_cards(pending_only=True):
            break
        await asyncio.sleep(0.01)
    cid = mgr.list_cards(pending_only=True)[0]["id"]
    card = mgr.decide_card(cid, ApprovalDecision.ALWAYS_ALLOW)
    ok, _, _ = await task
    assert ok is True
    # Downgraded to allow_once and NO personal allow_auto rule created
    assert card.status == CardStatus.ALLOW_ONCE
    assert card.saved_rule_id is None

    # Sending a new user message closes pending cards for that bot
    task_close = asyncio.create_task(
        mgr.request_approval(
            bot_id="bot-2",
            bot_name="Billing",
            action_name="stripe.charge",
            action_summary="Charge invoice #10",
        )
    )
    for _ in range(50):
        if mgr.list_cards(pending_only=True):
            break
        await asyncio.sleep(0.01)
    closed_count = mgr.close_open_cards_for_bot("bot-2")
    assert closed_count == 1
    ok_closed, _, card_closed = await task_close
    assert ok_closed is False and card_closed.status == CardStatus.CLOSED


@pytest.mark.asyncio
async def test_optional_model_reviewer_catches_subtle_risk(tmp_path):
    async def fake_reviewer(action_name, summary, detail, origin):
        if "secret-project" in summary.lower():
            return ReviewResult(outcome="ask", reason="Model flagged sensitive project leak")
        return None

    mgr = ApprovalManager(rules_path=tmp_path / "rules.json", model_reviewer=fake_reviewer)
    res = await mgr.review_action("notes.export", "Export secret-project notes")
    assert res.outcome == "ask"
    assert "Model flagged" in res.reason


# ---------------------------------------------------------------------------
# 3. FastAPI routes & waspctl approve CLI integration
# ---------------------------------------------------------------------------


def test_approval_routes_and_botctl_approve(tmp_path):
    app = FastAPI()
    runner = HarnessRunner(data_dir=tmp_path / "data", workspace_dir=tmp_path)
    mgr = ApprovalManager(rules_path=tmp_path / "data" / "approval_rules.json", broadcast=runner.broadcast)

    class _FakeAO:
        async def get_agent(self, aid):
            return Agent(id=aid, name="Athena") if aid == "bot-a" else None

        async def list_agents(self):
            return [Agent(id="bot-a", name="Athena")]

    app.state.harness_runner = runner
    app.state.approval_manager = mgr
    app.state.agent_orchestrator = _FakeAO()
    app.state.redis_url = ""
    app.include_router(harnesses_router, prefix="/harnesses")

    client = TestClient(app)

    # Create & list plain-English approval rules
    r_add = client.post(
        "/harnesses/api/approval-rules",
        json={"kind": "ask_first", "scope": "personal", "text": "Ask first before sending emails", "action_pattern": "gmail.send"},
    )
    assert r_add.status_code == 200
    rule_id = r_add.json()["rule"]["id"]
    assert len(client.get("/harnesses/api/approval-rules").json()["rules"]) == 1

    # Scoped botctl token approves safe action immediately
    tok = runner.issue_token("bot-a", "run-1")
    headers = {"Authorization": f"Bearer {tok}"}

    r_safe = client.post(
        "/harnesses/botctl/approve",
        headers=headers,
        json={"action_name": "fs.read", "action_summary": "Read local config"},
    )
    assert r_safe.status_code == 200
    assert r_safe.json()["approved"] is True
    assert r_safe.json()["auto"] is True

    # Delete rule works
    assert client.delete(f"/harnesses/api/approval-rules/{rule_id}").status_code == 200
    assert client.get("/harnesses/api/approval-rules").json()["rules"] == []


def test_waspctl_approve_cli_command(monkeypatch):
    calls: list[tuple[str, str, dict | None]] = []

    def fake_api(method, path, body=None, timeout=120.0):
        calls.append((method, path, body))
        if body and body.get("action_name") == "shell.rm":
            return {"ok": True, "approved": False, "reason": "Denied by operator"}
        return {"ok": True, "approved": True, "reason": "Auto-allowed"}

    monkeypatch.setenv("WASP_BOT_ID", "bot-a")
    monkeypatch.setattr(waspctl, "_api", fake_api)

    out = waspctl.run_cli(
        ["approve", "git.status", "Check git status", "--origin", "routine", "--detail", "/workspace", "--timeout", "60"]
    )
    assert "Auto-allowed" in out
    assert calls[0][1] == "/approve"
    assert calls[0][2]["origin"] == "routine"
    assert calls[0][2]["timeout"] == 60.0

    with pytest.raises(RuntimeError, match="Denied by operator"):
        waspctl.run_cli(["approve", "shell.rm", "Delete production folder"])
