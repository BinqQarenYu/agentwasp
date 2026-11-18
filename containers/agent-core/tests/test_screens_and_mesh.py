"""Unit and integration tests for Phase 3: Isolated `agent-screens` Container & Unified Mesh."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

repo_root = Path(__file__).resolve().parents[3]
core_dir = Path(__file__).resolve().parent.parent
if str(core_dir) not in sys.path:
    sys.path.insert(0, str(core_dir))

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.agent_manager.types import Agent, AgentStatus
from src.dashboard.routes.harnesses import router as harnesses_router
from src.dashboard.routes.mesh import router as mesh_router
from src.harnesses import HarnessRunner, ScreenClient, waspctl
from src.policy.approvals import ApprovalManager, RuleKind

# Load `containers/agent-screens/screen_daemon.py` directly for unit & live HTTP testing
_daemon_spec = importlib.util.spec_from_file_location(
    "screen_daemon",
    repo_root / "containers" / "agent-screens" / "screen_daemon.py",
)
assert _daemon_spec and _daemon_spec.loader
screen_daemon = importlib.util.module_from_spec(_daemon_spec)
sys.modules["screen_daemon"] = screen_daemon
_daemon_spec.loader.exec_module(screen_daemon)


class _FakeProc:
    def __init__(self, cmd: list[str], env: dict[str, str]) -> None:
        self.cmd = cmd
        self.env = env
        self.alive = True
        self.terminated = False

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.terminated = True
        self.alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.alive = False


# ---------------------------------------------------------------------------
# 1. ScreenManager: slot limits, LRU eviction, busy guard, idle reaper
# ---------------------------------------------------------------------------


def test_screen_manager_lru_eviction_busy_guard_and_reaper(tmp_path):
    spawned: list[_FakeProc] = []

    def fake_spawn(cmd, env):
        p = _FakeProc(cmd, env)
        spawned.append(p)
        return p

    available_bins = {"Xvfb", "dbus-daemon", "openbox", "x11vnc", "websockify"}
    mgr = screen_daemon.ScreenManager(
        profiles_root=tmp_path / "profiles",
        run_dir=tmp_path / "run",
        max_screens=2,
        idle_minutes=1.0,
        spawner=fake_spawn,
        which_fn=lambda name: f"/usr/bin/{name}" if name in available_bins else None,
        wait_for_display=False,
    )

    s1 = mgr.ensure("bot-1", busy=False)
    assert s1["slot"] == 1
    assert s1["display"] == ":101"
    assert s1["backend"] == "novnc"
    assert "/screen/6901/vnc.html" in s1["view_url"]

    s2 = mgr.ensure("bot-2", busy=True)
    assert s2["slot"] == 2
    assert len(mgr.list_screens()) == 2

    # 3rd bot evicts idle LRU (`bot-1`), preserving busy `bot-2`
    s3 = mgr.ensure("bot-3", busy=True)
    assert s3["slot"] == 1
    active_ids = {s["bot_id"] for s in mgr.list_screens()}
    assert active_ids == {"bot-2", "bot-3"}

    # All 2 slots are now busy -> 4th bot fails cleanly instead of killing an active bot
    with pytest.raises(RuntimeError, match="currently busy"):
        mgr.ensure("bot-4")

    # Mark bot-3 idle and trigger reaper in the future -> reaped cleanly
    mgr.set_busy("bot-3", False)
    reaped = mgr.reap_idle(now=time.monotonic() + 120.0)
    assert reaped == ["bot-3"]
    assert {s["bot_id"] for s in mgr.list_screens()} == {"bot-2"}

    # Explicit close releases slot
    assert mgr.close("bot-2") is True
    assert mgr.list_screens() == []


# ---------------------------------------------------------------------------
# 2. Live HTTP roundtrip: ScreenClient <-> ScreenDaemon + Handoff screen_url
# ---------------------------------------------------------------------------


@pytest.fixture
def live_screen_server(tmp_path):
    available_bins = {"Xvfb", "voxl"}
    mgr = screen_daemon.ScreenManager(
        profiles_root=tmp_path / "profiles",
        run_dir=tmp_path / "run",
        max_screens=2,
        public_base="http://127.0.0.1:1434",
        spawner=lambda cmd, env: _FakeProc(cmd, env),
        which_fn=lambda name: f"/usr/bin/{name}" if name in available_bins else None,
        wait_for_display=False,
    )
    server = screen_daemon.make_http_server(
        mgr,
        host="127.0.0.1",
        port=0,  # OS-assigned ephemeral free port
        internal_token="secret-screen-token",
    )
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}", "secret-screen-token", mgr
    server.shutdown()
    server.server_close()


@pytest.mark.asyncio
async def test_screen_client_live_roundtrip_auth_and_offline_fallback(live_screen_server):
    base_url, token, _ = live_screen_server

    # Bad token rejected cleanly
    bad_client = ScreenClient(base_url=base_url, internal_token="wrong-token")
    res_bad = await bad_client.ensure_screen("bot-1")
    assert res_bad["ok"] is False and res_bad["available"] is True

    # Valid client opens, inspects env, lists, and closes screen
    client = ScreenClient(base_url=base_url, internal_token=token)
    health = await client.health()
    assert health["ok"] is True and health["available"] is True and health["has_voxl"] is True

    opened = await client.ensure_screen("bot-1", busy=True)
    assert opened["ok"] is True
    screen = opened["screen"]
    assert screen["backend"] == "voxl"
    assert screen["view_url"].startswith("http://127.0.0.1:1434/screen/6901/?token=")

    env = ScreenClient.build_bot_screen_env(screen)
    assert env["DISPLAY"] == ":101"
    assert env["AGENT_BROWSER_SESSION"] == "wasp-bot-1"
    assert env["AGENT_BROWSER_HEADED"] == "1"

    closed = await client.close_screen("bot-1")
    assert closed["ok"] is True

    # Offline fallback never crashes
    offline = ScreenClient(base_url="http://127.0.0.1:1", timeout=0.3)
    off_health = await offline.health()
    assert off_health["ok"] is False and off_health["available"] is False


def test_botctl_screen_and_handoff_attaches_live_screen_url(tmp_path, live_screen_server):
    base_url, token, _ = live_screen_server
    app = FastAPI()
    runner = HarnessRunner(data_dir=tmp_path / "data", workspace_dir=tmp_path)
    sc = ScreenClient(base_url=base_url, internal_token=token)

    app.state.harness_runner = runner
    app.state.approval_manager = ApprovalManager(rules_path=tmp_path / "rules.json", broadcast=runner.broadcast)
    app.state.screen_client = sc
    app.state.agent_orchestrator = None
    app.state.redis_url = ""
    app.include_router(harnesses_router, prefix="/harnesses")

    client = TestClient(app)
    tok = runner.issue_token("bot-07", "run-screen")
    headers = {"Authorization": f"Bearer {tok}"}

    # botctl screen endpoint
    r_sc = client.post("/harnesses/botctl/screen", headers=headers)
    assert r_sc.status_code == 200
    assert r_sc.json()["screen"]["backend"] == "voxl"

    # Control-plane list & close
    r_list = client.get("/harnesses/api/screens")
    assert r_list.status_code == 200 and len(r_list.json()["screens"]) == 1

    # waspctl screen CLI formatting
    original_api = waspctl._api
    try:
        waspctl._api = lambda method, path, body=None, timeout=30.0: {"ok": True, "screen": {"view_url": "http://x/screen/6901/"}}
        out = waspctl.run_cli(["screen"])
        assert "6901" in out
    finally:
        waspctl._api = original_api


# ---------------------------------------------------------------------------
# 3. Unified Mesh Cockpit State (consolidated with agent_orchestrator + approvals + screens)
# ---------------------------------------------------------------------------


def test_mesh_state_unifies_live_orchestrator_approvals_and_screens(tmp_path, live_screen_server):
    base_url, token, _ = live_screen_server
    app = FastAPI()
    runner = HarnessRunner(data_dir=tmp_path / "data", workspace_dir=tmp_path)
    approvals = ApprovalManager(rules_path=tmp_path / "rules.json", broadcast=runner.broadcast)
    approvals.add_rule(kind=RuleKind.ASK_FIRST, text="Ask before production deploys")
    approvals.add_rule(kind=RuleKind.ALLOW_AUTO, text='Allow "git status"')
    sc = ScreenClient(base_url=base_url, internal_token=token)

    class _LiveAO:
        async def list_agents(self):
            return [
                Agent(id="bot-01", name="Athena", harness="claude", status=AgentStatus.RUNNING),
                Agent(id="bot-02", name="Mue", harness="", status=AgentStatus.IDLE),
            ]

    app.state.agent_orchestrator = _LiveAO()
    app.state.harness_runner = runner
    app.state.approval_manager = approvals
    app.state.screen_client = sc
    app.state.memory = MagicMock(get_stats=lambda: {"total": 250})
    app.include_router(mesh_router, prefix="/mesh")

    client = TestClient(app)
    resp = client.get("/mesh/api/state")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["roster_source"] == "agent_orchestrator"
    assert data["bots_online"] == 2
    assert data["bots"][0]["name"] == "Athena"
    assert data["bots"][0]["harness"] == "claude"
    assert data["active_rules"] == 2
    assert data["screens"]["available"] is True
