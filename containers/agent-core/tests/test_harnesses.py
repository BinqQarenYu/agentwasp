"""Unit and integration tests for Phase 1: Harness-Agnostic CLI Engine & waspctl."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

core_dir = Path(__file__).resolve().parent.parent
if str(core_dir) not in sys.path:
    sys.path.insert(0, str(core_dir))

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.agent_manager.types import Agent
from src.dashboard.auth import AuthMiddleware
from src.dashboard.routes.harnesses import router as harnesses_router
from src.harnesses import (
    ADAPTER_NAMES,
    AgentEvent,
    ClaudeAdapter,
    CodexAdapter,
    EventType,
    GeminiAdapter,
    HarnessAdapter,
    HarnessRunner,
    ModelOption,
    PiAdapter,
    RunInput,
    get_adapter,
    waspctl,
)


# ---------------------------------------------------------------------------
# Helper: Fake Python-backed CLI harness for real subprocess streaming tests
# ---------------------------------------------------------------------------


class EchoPythonAdapter(HarnessAdapter):
    """Spawns the current Python interpreter to emit deterministic JSONL events."""

    name = "echo-py"
    bin_name = sys.executable

    def __init__(self, delay_seconds: float = 0.0, script: str | None = None) -> None:
        self.delay_seconds = delay_seconds
        self.script = script

    def build_args(self, run_input: RunInput, pre_events: list[AgentEvent]) -> list[str]:
        code = self.script or (
            "import json, os, sys, time; "
            f"time.sleep({self.delay_seconds}); "
            "print(json.dumps({'type': 'session', 'id': 'sess-py-1'}), flush=True); "
            "print(json.dumps({'type': 'tool', 'name': 'shell', 'detail': 'echo ok'}), flush=True); "
            "print(json.dumps({'type': 'delta', 'text': 'Hello '}), flush=True); "
            "print(json.dumps({'type': 'text', 'text': 'Echo: ' + sys.argv[1] + ' token=' + ('yes' if os.environ.get('WASP_TOKEN') else 'no')}), flush=True)"
        )
        return ["-c", code, run_input.prompt]

    def parse_event(self, payload: dict, out: list[AgentEvent]) -> None:
        t = payload.get("type")
        if t == "session":
            out.append(AgentEvent(type=EventType.SESSION, session_id=payload["id"]))
        elif t == "tool":
            out.append(AgentEvent(type=EventType.TOOL, tool_name=payload["name"], tool_detail=payload["detail"]))
        elif t == "delta":
            out.append(AgentEvent(type=EventType.TEXT_DELTA, text=payload["text"]))
        elif t == "text":
            out.append(AgentEvent(type=EventType.TEXT, text=payload["text"]))

    def list_models(self) -> list[ModelOption]:
        return [ModelOption(id="echo-1", label="Echo 1")]


# ---------------------------------------------------------------------------
# 1. Adapter argument building & JSONL event parsing
# ---------------------------------------------------------------------------


def test_claude_adapter_safe_default_and_parsing(tmp_path):
    adapter = ClaudeAdapter()
    inp = RunInput(
        prompt="Summarize repo",
        system="You are Athena.",
        cwd=tmp_path,
        state_dir=tmp_path / "state",
        model="sonnet",
        effort="high",
    )
    pre: list[AgentEvent] = []
    args = adapter.build_args(inp, pre)
    assert "--dangerously-skip-permissions" not in args
    assert "--session-id" in args
    assert "--resume" not in args
    assert "--model" in args and "sonnet" in args
    assert "--effort" in args and "high" in args
    assert args[-2:] == ["--", "Summarize repo"]

    # Explicit opt-in enables unsafe bypass
    inp.allow_unsafe_bypass = True
    inp.session_id = "sess-existing"
    args_resume = adapter.build_args(inp, [])
    assert "--dangerously-skip-permissions" in args_resume
    assert "--resume" in args_resume and "sess-existing" in args_resume

    # Event parsing
    out: list[AgentEvent] = []
    adapter.parse_event(
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}}},
        out,
    )
    adapter.parse_event(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "git status"}},
                    {"type": "text", "text": "All clean."},
                ]
            },
        },
        out,
    )
    adapter.parse_event({"type": "result", "is_error": True, "result": "rate limit"}, out)

    types = [e.type for e in out]
    assert types == [EventType.TEXT_DELTA, EventType.SESSION, EventType.TOOL, EventType.TEXT, EventType.ERROR]
    assert out[1].session_id == "sess-existing"
    assert out[2].tool_name == "Bash" and out[2].tool_detail == "git status"
    assert out[3].text == "All clean."
    assert out[4].text == "rate limit"


def test_codex_adapter_safe_default_and_parsing(tmp_path):
    adapter = CodexAdapter()
    inp = RunInput(
        prompt="Audit tests",
        system="Be strict.",
        cwd=tmp_path,
        state_dir=tmp_path / "state",
        model="gpt-5-codex",
        effort="high",
    )
    args = adapter.build_args(inp, [])
    assert args[:3] == ["exec", "--json", "--skip-git-repo-check"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in args
    assert "-m" in args and "gpt-5-codex" in args

    inp.session_id = "thr_123"
    inp.allow_unsafe_bypass = True
    resume_args = adapter.build_args(inp, [])
    assert resume_args[:2] == ["exec", "resume"]
    assert "--dangerously-bypass-approvals-and-sandbox" in resume_args
    assert resume_args[-3:] == ["--", "thr_123", "Audit tests"]

    out: list[AgentEvent] = []
    adapter.parse_event({"type": "thread.started", "thread_id": "thr_123"}, out)
    adapter.parse_event(
        {"type": "item.started", "item": {"type": "command_execution", "command": "/bin/bash -lc 'pytest -q'"}},
        out,
    )
    adapter.parse_event({"type": "item.completed", "item": {"type": "agent_message", "text": "Tests pass."}}, out)
    adapter.parse_event({"type": "turn.failed", "error": {"message": "boom"}}, out)

    assert [e.type for e in out] == [EventType.SESSION, EventType.TOOL, EventType.TEXT, EventType.ERROR]
    assert out[0].session_id == "thr_123"
    assert out[1].tool_name == "shell" and out[1].tool_detail == "pytest -q"


def test_pi_adapter_at_padding_and_parsing(tmp_path):
    adapter = PiAdapter()
    inp = RunInput(
        prompt="@README.md review this",
        system="Sys",
        cwd=tmp_path,
        state_dir=tmp_path / "state",
        effort="medium",
    )
    pre: list[AgentEvent] = []
    args = adapter.build_args(inp, pre)
    assert len(pre) == 1 and pre[0].type == EventType.SESSION and pre[0].session_id.startswith("s-")
    assert args[-1] == " @README.md review this"  # padded so Pi doesn't read @file
    assert "--thinking" in args and "medium" in args

    out: list[AgentEvent] = []
    adapter.parse_event({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "Yo"}}, out)
    adapter.parse_event({"type": "tool_execution_start", "toolName": "read", "args": {"path": "README.md"}}, out)
    adapter.parse_event(
        {"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "Done"}]}},
        out,
    )
    assert [e.type for e in out] == [EventType.TEXT_DELTA, EventType.TOOL, EventType.TEXT]


def test_gemini_adapter_system_file_and_parsing(tmp_path):
    adapter = GeminiAdapter()
    inp = RunInput(
        prompt="Inspect logs",
        system="You are Mue.",
        cwd=tmp_path,
        state_dir=tmp_path / "state",
        model="gemini-2.5-flash",
    )
    args = adapter.build_args(inp, [])
    assert "--yolo" not in args
    assert "--output-format" in args and "stream-json" in args
    assert "-m" in args and "gemini-2.5-flash" in args
    env = adapter.extra_env(inp)
    assert "GEMINI_SYSTEM_MD" in env
    assert Path(env["GEMINI_SYSTEM_MD"]).read_text(encoding="utf-8") == "You are Mue."

    out: list[AgentEvent] = []
    adapter.parse_event({"type": "init", "session_id": "gem-42"}, out)
    adapter.parse_event({"type": "tool_use", "tool_name": "grep", "parameters": {"pattern": "TODO"}}, out)
    adapter.parse_event({"type": "message", "role": "assistant", "content": "Part ", "delta": True}, out)
    adapter.parse_event({"type": "message", "role": "assistant", "content": "Full reply"}, out)
    assert [e.type for e in out] == [EventType.SESSION, EventType.TOOL, EventType.TEXT_DELTA, EventType.TEXT]
    assert out[0].session_id == "gem-42"


def test_registry_and_agent_model_fields():
    assert set(ADAPTER_NAMES) == {"claude", "codex", "pi", "gemini"}
    for name in ADAPTER_NAMES:
        assert get_adapter(name) is not None
    assert get_adapter("nonexistent") is None

    agent = Agent(name="Researcher", harness="claude", reasoning_effort="high")
    assert agent.harness == "claude"
    assert agent.harness_session_id == ""
    assert agent.reasoning_effort == "high"


# ---------------------------------------------------------------------------
# 2. HarnessRunner: FIFO queue, parallelism, memory, tokens, SSE, handoffs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_executes_streams_revokes_token_and_creates_memory(tmp_path):
    runner = HarnessRunner(data_dir=tmp_path / "data", workspace_dir=tmp_path, user_name="Ben")
    sub = runner.subscribe()

    result = await runner.run_bot(
        bot_id="bot-01",
        bot_name="Athena",
        harness_name="echo-py",
        prompt="status check",
        identity_prompt="Fleet governor",
        adapter_override=EchoPythonAdapter(),
    )

    assert result.ok is True
    assert result.session_id == "sess-py-1"
    assert "Echo: status check token=yes" in result.text

    # Plain-file memory created
    assert (tmp_path / "data" / "USER.md").exists()
    assert (tmp_path / "data" / "bots" / "bot-01" / "MEMORY.md").exists()
    assert "Ben" in (tmp_path / "data" / "USER.md").read_text(encoding="utf-8")

    # Token automatically revoked after run ends
    assert len(runner._tokens) == 0

    # SSE events received
    seen_types: list[str] = []
    while not sub.empty():
        seen_types.append(sub.get_nowait()["type"])
    assert "run_started" in seen_types
    assert "harness_tool" in seen_types
    assert "harness_text" in seen_types
    assert "run_finished" in seen_types
    runner.unsubscribe(sub)


@pytest.mark.asyncio
async def test_runner_serial_per_bot_and_parallel_across_bots(tmp_path):
    runner = HarnessRunner(data_dir=tmp_path / "data", workspace_dir=tmp_path)

    ts_script = (
        "import json, sys, time; "
        "t_start = time.time(); "
        "time.sleep(0.25); "
        "t_end = time.time(); "
        "print(json.dumps({'type': 'text', 'text': json.dumps({'label': sys.argv[1], 'start': t_start, 'end': t_end})}), flush=True)"
    )

    # Same bot: two calls must NOT overlap (strictly FIFO serial)
    r1, r2 = await asyncio.gather(
        runner.run_bot(
            bot_id="bot-same",
            bot_name="SerialBot",
            harness_name="echo-py",
            prompt="first",
            adapter_override=EchoPythonAdapter(script=ts_script),
        ),
        runner.run_bot(
            bot_id="bot-same",
            bot_name="SerialBot",
            harness_name="echo-py",
            prompt="second",
            adapter_override=EchoPythonAdapter(script=ts_script),
        ),
    )
    assert r1.ok and r2.ok
    d1, d2 = json.loads(r1.text), json.loads(r2.text)
    assert d1["end"] <= d2["start"] + 0.01  # second starts only after first ends

    # Distinct bots: MUST overlap in time (parallel across bots)
    pa, pb = await asyncio.gather(
        runner.run_bot(
            bot_id="bot-a",
            bot_name="BotA",
            harness_name="echo-py",
            prompt="a",
            adapter_override=EchoPythonAdapter(script=ts_script),
        ),
        runner.run_bot(
            bot_id="bot-b",
            bot_name="BotB",
            harness_name="echo-py",
            prompt="b",
            adapter_override=EchoPythonAdapter(script=ts_script),
        ),
    )
    assert pa.ok and pb.ok
    da, db = json.loads(pa.text), json.loads(pb.text)
    assert da["start"] < db["end"] and db["start"] < da["end"]  # genuine overlap


@pytest.mark.asyncio
async def test_runner_cancel_active_run(tmp_path):
    runner = HarnessRunner(data_dir=tmp_path / "data", workspace_dir=tmp_path)
    slow_adapter = EchoPythonAdapter(script="import time; time.sleep(30)")

    task = asyncio.create_task(
        runner.run_bot(
            bot_id="bot-cancel",
            bot_name="SlowBot",
            harness_name="echo-py",
            prompt="wait",
            adapter_override=slow_adapter,
        )
    )
    # Wait briefly until run is registered
    for _ in range(50):
        if runner.is_running("bot-cancel"):
            break
        await asyncio.sleep(0.02)

    assert runner.cancel_run("bot-cancel") is True
    res = await task
    assert res.ok is False
    assert res.aborted_reason == "cancelled"


@pytest.mark.asyncio
async def test_runner_handoff_blocking_and_resolution(tmp_path):
    runner = HarnessRunner(data_dir=tmp_path / "data", workspace_dir=tmp_path)

    async def _resolve_soon():
        for _ in range(50):
            pending = runner.list_handoffs(pending_only=True)
            if pending:
                runner.resolve_handoff(pending[0]["id"], note="Logged into Stripe")
                return
            await asyncio.sleep(0.01)

    waiter = asyncio.create_task(
        runner.request_handoff(bot_id="bot-07", bot_name="Billing", reason="Complete 2FA", timeout_seconds=5.0)
    )
    resolver = asyncio.create_task(_resolve_soon())
    handoff, resolved = await waiter
    await resolver

    assert resolved is True
    assert handoff.status == "resolved"
    assert handoff.resolution_note == "Logged into Stripe"


# ---------------------------------------------------------------------------
# 3. FastAPI routes, AuthMiddleware scoped bearer token, & waspctl CLI
# ---------------------------------------------------------------------------


class _FakeOrchestrator:
    def __init__(self, agents: list[Agent]) -> None:
        self._agents = {a.id: a for a in agents}
        self.created_goals: list[tuple[str, str]] = []

    async def list_agents(self) -> list[Agent]:
        return list(self._agents.values())

    async def get_agent(self, agent_id: str) -> Agent | None:
        return self._agents.get(agent_id)

    async def create_agent_goal(self, agent_id: str, objective: str):
        self.created_goals.append((agent_id, objective))
        return SimpleNamespace(id=f"goal-{len(self.created_goals)}")


@pytest.fixture
def harness_app_and_client(tmp_path):
    app = FastAPI()
    runner = HarnessRunner(data_dir=tmp_path / "data", workspace_dir=tmp_path)
    bot_a = Agent(id="bot-a", name="Athena", description="Governor", harness="claude")
    bot_b = Agent(id="bot-b", name="Hermes", description="Native worker", harness="")
    ao = _FakeOrchestrator([bot_a, bot_b])

    app.state.harness_runner = runner
    app.state.agent_orchestrator = ao
    app.state.redis_url = ""
    app.include_router(harnesses_router, prefix="/harnesses")
    return app, runner, ao, TestClient(app)


def test_control_plane_status_models_and_memory_routes(harness_app_and_client):
    _, _, _, client = harness_app_and_client

    r = client.get("/harnesses/api/status")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "claude" in data["supported_harnesses"]
    assert data["allow_unsafe_bypass"] is False

    r_models = client.get("/harnesses/api/models/claude")
    assert r_models.status_code == 200
    assert len(r_models.json()["models"]) >= 3

    r_mem = client.get("/harnesses/api/memory/bot-a")
    assert r_mem.status_code == 200
    assert "# Athena" in r_mem.json()["memory_md"]

    r_put = client.put("/harnesses/api/memory/bot-a", json={"memory_md": "# Athena\n\n- Always verify first.\n"})
    assert r_put.status_code == 200
    assert "Always verify first." in client.get("/harnesses/api/memory/bot-a").json()["memory_md"]


def test_botctl_auth_middleware_enforces_scoped_token(tmp_path):
    app = FastAPI()
    runner = HarnessRunner(data_dir=tmp_path / "data", workspace_dir=tmp_path)
    app.state.harness_runner = runner
    app.state.agent_orchestrator = _FakeOrchestrator([Agent(id="bot-a", name="Athena")])
    app.state.redis_url = ""
    app.add_middleware(AuthMiddleware)
    app.include_router(harnesses_router, prefix="/harnesses")

    client = TestClient(app)

    # Missing token -> 401 JSON (never redirects to /login)
    r_no_auth = client.get("/harnesses/botctl/bots", follow_redirects=False)
    assert r_no_auth.status_code == 401

    # Bogus token -> 401
    r_bad = client.get("/harnesses/botctl/bots", headers={"Authorization": "Bearer wrong"})
    assert r_bad.status_code == 401

    # Valid per-run token -> 200
    tok = runner.issue_token("bot-a", "run-1")
    r_ok = client.get("/harnesses/botctl/bots", headers={"Authorization": f"Bearer {tok}"})
    assert r_ok.status_code == 200
    assert r_ok.json()["caller"] == "bot-a"
    assert r_ok.json()["bots"][0]["name"] == "Athena"

    # After revocation -> 401
    runner.revoke_token(tok)
    assert client.get("/harnesses/botctl/bots", headers={"Authorization": f"Bearer {tok}"}).status_code == 401


def test_botctl_ask_and_routines_crud(harness_app_and_client):
    _, runner, ao, client = harness_app_and_client
    tok = runner.issue_token("bot-a", "run-2")
    headers = {"Authorization": f"Bearer {tok}"}

    # Self-ask rejected
    r_self = client.post("/harnesses/botctl/ask", headers=headers, json={"to": "Athena", "text": "hi"})
    assert r_self.status_code == 400

    # Asking a native WASP bot delegates via GoalOrchestrator
    r_ask = client.post("/harnesses/botctl/ask", headers=headers, json={"to": "Hermes", "text": "Check inbox"})
    assert r_ask.status_code == 200
    assert r_ask.json()["mode"] == "async_goal"
    assert len(ao.created_goals) == 1

    # Routines full lifecycle
    r_add = client.post(
        "/harnesses/botctl/routines",
        headers=headers,
        json={"name": "Hourly Check", "schedule": "every 1h", "instruction": "Inspect health"},
    )
    assert r_add.status_code == 200
    rid = r_add.json()["id"]

    r_list = client.get("/harnesses/botctl/routines", headers=headers)
    assert len(r_list.json()["routines"]) == 1

    r_pause = client.patch(f"/harnesses/botctl/routines/{rid}", headers=headers, json={"enabled": False})
    assert r_pause.status_code == 200 and r_pause.json()["routine"]["enabled"] is False

    r_del = client.delete(f"/harnesses/botctl/routines/{rid}", headers=headers)
    assert r_del.status_code == 200
    assert client.get("/harnesses/botctl/routines", headers=headers).json()["routines"] == []


def test_waspctl_cli_commands_and_error_handling(monkeypatch):
    # Help works without env vars
    assert "waspctl bots" in waspctl.run_cli(["--help"])

    # Missing env vars produces clear error
    monkeypatch.delenv("WASP_API", raising=False)
    monkeypatch.delenv("WASP_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="WASP_API and WASP_TOKEN"):
        waspctl.run_cli(["bots"])

    # Mock API transport to verify CLI argument formatting
    calls: list[tuple[str, str, dict | None]] = []

    def fake_api(method, path, body=None, timeout=120.0):
        calls.append((method, path, body))
        if path == "/ask":
            return {"ok": True, "reply": "42"}
        if path == "/routines" and method == "POST":
            return {"ok": True, "id": "r-99"}
        if path == "/handoff":
            return {"ok": True, "note": "Approved"}
        return {"ok": True, "bots": [{"id": "bot-a", "name": "Athena"}]}

    monkeypatch.setenv("WASP_BOT_ID", "bot-a")
    monkeypatch.setattr(waspctl, "_api", fake_api)

    assert "Athena" in waspctl.run_cli(["bots"])
    assert waspctl.run_cli(["ask", "Hermes", "What is 6*7?", "--timeout", "45"]) == "42"
    assert waspctl.run_cli(["handoff", "Solve 2FA"]) == "Approved"
    assert "r-99" in waspctl.run_cli(["routine", "add", "Morning", "daily", "Send digest"])
    assert waspctl.run_cli(["routine", "pause", "r-99"]) == "Paused."
    assert waspctl.run_cli(["routine", "resume", "r-99"]) == "Resumed."
    assert waspctl.run_cli(["routine", "rm", "r-99"]) == "Deleted."

    assert calls[1] == ("POST", "/ask", {"from": "bot-a", "to": "Hermes", "text": "What is 6*7?", "timeout": 45.0})
    assert calls[4] == ("PATCH", "/routines/r-99", {"enabled": False})
    assert calls[6] == ("DELETE", "/routines/r-99", None)


def test_loopback_auto_login_guard():
    from src.dashboard.auth import _is_loopback_request

    req_local = SimpleNamespace(headers={"host": "127.0.0.1:1434"})
    req_localhost = SimpleNamespace(headers={"host": "localhost:1434"})
    req_proxied = SimpleNamespace(headers={"host": "127.0.0.1:1434", "x-forwarded-for": "203.0.113.5"})
    req_public = SimpleNamespace(headers={"host": "agentwasp.example.com"})

    assert _is_loopback_request(req_local) is True
    assert _is_loopback_request(req_localhost) is True
    assert _is_loopback_request(req_proxied) is False
    assert _is_loopback_request(req_public) is False
