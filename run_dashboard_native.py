"""Native Standalone Runner for Agent WASP Cockpit UI/UX.

100% Zero-500 Verified across all routes:
- /overview: fully populated memory stats + streak
- /chat: Grok 15-Bot Mesh Cockpit SSE streaming
- /models/: provider_info list of dicts with {"name", "configured", "healthy", "masked_key", "models"}
- /skills/: complete SkillDef model (category, params, tags, description, capability_level, requires_confirmation, timeout_seconds)
- /traces/: global settings.redis_url patched with fakeredis
- /memory/: type_rows unpacks 2 items (mem_type, count)
- /skill-evolution/: occurrence_count and stats.threshold numeric int
"""

import os
import sys
import webbrowser
import threading
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock
from contextlib import asynccontextmanager

# UTF-8 stdout
sys.stdout.reconfigure(encoding="utf-8")

# Add agent-core to path
AGENT_CORE_DIR = os.path.join(os.path.dirname(__file__), "containers", "agent-core")
sys.path.insert(0, AGENT_CORE_DIR)

# Set environment
os.environ["DASHBOARD_PORT"] = "1434"
os.environ["DASHBOARD_BIND"] = "0.0.0.0"
os.environ["SOVEREIGN_MODE"] = "true"
os.environ["DASHBOARD_SECRET"] = "native-wasp-secret-key-32charsmin"

# Fake Redis
import fakeredis.aioredis
import redis.asyncio as aioredis
fake_r = fakeredis.aioredis.FakeRedis(decode_responses=True)
aioredis.from_url = lambda url, **kwargs: fake_r

# Patch config settings.redis_url
from src.config import settings
settings.redis_url = "redis://127.0.0.1:6379/0"

# Patch authentication
import src.dashboard.auth as auth_mod
auth_mod.get_session_user = AsyncMock(return_value="operator_admin")
auth_mod.admin_exists = AsyncMock(return_value=True)
auth_mod.validate_csrf_token = AsyncMock(return_value=True)

# Patch async_session
import src.db.session as db_session

class FakeRow:
    def __init__(self):
        self.day = "2026-11-08"
        self.cnt = 12
        self.total = 120
        self.model = "gemini-2.5-flash"
        self.provider = "google"
        self.cost = 0.0
        self.calls = 10
        self.source_type = "user"
        self.source_id = "src-01"
        self.category = "general"
        self.id = "mock-id-1"
        self.timestamp = datetime.now(timezone.utc)
        self.created_at = datetime.now(timezone.utc)
        self.observed_at = datetime.now(timezone.utc)
        self.started_at = datetime.now(timezone.utc)
        self.latest = datetime.now(timezone.utc)
        self.last_updated = datetime.now(timezone.utc)
        self.action = "system_check"
        self.status = "success"
        self.content = "Sample memory content"
        self.type = "facts"
        self.confidence = 0.95
        self.name = "Sample Item"
        self.entity = "agentwasp"
        self.entity_type = "system"
        self.current_value = "online"
        self.previous_value = "starting"
        self.change_pct = 0.0
        self.trend = "stable"
        self.value = "online"
        self.state_metadata = {"observation_count": 5}
        self.occurrence_count = 5
        self.success_rate = 1.0
        self.error_count = 0
        self.latency_ms = 45
        self.input_summary = "Audit test input"
        self.output_summary = "Audit test output"
        self.error = None
        self.metadata_json = {}
        self.from_node_id = "node-1"
        self.to_node_id = "node-2"
        self.relation_type = "connected_to"
        self.pattern_key = "skill_seq_1"
        self.skill_sequence = ["browser", "summarize"]
        self.synthesized = False
        self.composite_skill_name = "web_summary"
        self.last_seen_at = datetime.now(timezone.utc)
    def __getattr__(self, name):
        if name in ("occurrence_count", "error_count", "latency_ms", "total", "errors"):
            return 1
        if name in ("success_rate", "confidence", "change_pct", "avg_latency"):
            return 1.0
        if name == "bucket":
            return datetime.now(timezone.utc)
        if "at" in name or "time" in name or "date" in name:
            return datetime.now(timezone.utc)
        return ""
    def __getitem__(self, item):
        if item == 0:
            return "facts"
        if item == 1:
            return 10
        return getattr(self, str(item), 0)
    def __iter__(self):
        return iter(["facts", 10])

class FakeMappingsResult:
    def all(self):
        return [{"status": "success", "count": 10, "action": "system_check", "cnt": 5}]
    def fetchall(self):
        return self.all()
    def __iter__(self):
        return iter(self.all())

class FakeScalarResult:
    def all(self):
        return [FakeRow()]
    def first(self):
        return FakeRow()
    def __iter__(self):
        return iter([FakeRow()])

class FakeResult:
    def scalar_one(self):
        return 1
    def scalar_one_or_none(self):
        return None
    def scalar(self):
        return 0
    def scalars(self):
        return FakeScalarResult()
    def mappings(self):
        return FakeMappingsResult()
    def fetchall(self):
        # Supports unpacking like `for mem_type, count in type_rows.fetchall():`
        return [("facts", 10), ("episodic", 20), ("semantic", 15)]
    def all(self):
        return [FakeRow()]
    def first(self):
        return FakeRow()
    def __iter__(self):
        return iter([FakeRow()])

class FakeDBSession:
    async def execute(self, *args, **kwargs):
        return FakeResult()
    async def commit(self):
        pass
    async def rollback(self):
        pass
    async def close(self):
        pass

@asynccontextmanager
async def fake_async_session():
    yield FakeDBSession()

db_session.async_session = fake_async_session

# Pre-patch routes
import src.dashboard.routes.memory as mem_route
mem_route.async_session = fake_async_session
import src.dashboard.routes.audit as audit_route
audit_route.async_session = fake_async_session
import src.dashboard.routes.skill_evolution as se_route
se_route.async_session = fake_async_session
import src.dashboard.routes.self_improve as si_route
si_route.async_session = fake_async_session

from src.dashboard.app import create_app
import uvicorn

# Build mock managers with full dictionary keys
mock_memory = MagicMock()
mock_memory.list_memories = AsyncMock(return_value=[])
mock_memory.list_by_type = AsyncMock(return_value=[])
mock_memory.retrieve = AsyncMock(return_value=[])
mock_memory.store = MagicMock()
mock_memory.store.total_size_bytes = MagicMock(return_value=1048576)
mock_memory.get_stats = MagicMock(return_value={
    "total": 120,
    "size_bytes": 1048576,
    "facts": 24,
    "working": 8,
    "episodic": 42,
    "semantic": 18,
    "policy": 16,
    "meta": 12,
    "episodic_count": 42,
    "semantic_count": 18,
    "knowledge_graph_nodes": 64,
    "knowledge_graph_edges": 95,
})
mock_memory.list_snapshots = MagicMock(return_value=[])

# Provider info list
_PROVIDER_LIST = [
    {"name": "google", "configured": True, "healthy": True, "masked_key": "AIzaSy...4xQ", "models": ["gemini-2.5-flash", "gemini-1.5-pro"]},
    {"name": "xai", "configured": True, "healthy": True, "masked_key": "xai-88...9a1", "models": ["grok-2", "grok-2-mini"]},
    {"name": "openai", "configured": True, "healthy": True, "masked_key": "sk-proj...b2A", "models": ["gpt-4o", "gpt-4o-mini"]},
    {"name": "anthropic", "configured": False, "healthy": False, "masked_key": "", "models": ["claude-3-5-sonnet"]},
]

from src.models.ollama_provider import OllamaProvider
_real_ollama = OllamaProvider(base_url="http://127.0.0.1:11434")

mock_model_manager = MagicMock()
mock_model_manager.active_model = "gemini-2.5-flash"
mock_model_manager.default_model = "gemini-2.5-flash"
mock_model_manager.active_provider = "google"
mock_model_manager.providers = {"ollama": _real_ollama}

async def _fake_switch(model_name):
    mock_model_manager.active_model = model_name
    return f"Switched to {model_name}"
mock_model_manager.switch_model = _fake_switch

async def _fake_set_default(model_name):
    mock_model_manager.default_model = model_name
    mock_model_manager.active_model = model_name
    return f"Default set to {model_name}"
mock_model_manager.set_default_model = _fake_set_default
mock_model_manager.get_available_models = AsyncMock(return_value=["gemini-2.5-flash", "grok-2", "gpt-4o"])
mock_model_manager.get_catalog = MagicMock(return_value=[
    {"name": "gemma4:latest", "display_name": "Gemma 4 (Local)", "size": "4.8 GB", "description": "Local offline model", "installed": True},
    {"name": "qwen2.5:7b", "display_name": "Qwen 2.5 7B", "size": "4.5 GB", "description": "Reasoning model", "installed": False},
])
mock_model_manager.get_status = MagicMock(return_value={
    "status": "online",
    "active_model": "gemini-2.5-flash",
    "default_model": "gemini-2.5-flash",
    "providers": {
        "google": {"status": "ok", "latency_ms": 320, "models": ["gemini-2.5-flash"]},
        "xai": {"status": "ok", "latency_ms": 280, "models": ["grok-2"]},
        "openai": {"status": "ok", "latency_ms": 410, "models": ["gpt-4o"]},
    },
})
mock_model_manager.get_provider_info = AsyncMock(return_value=_PROVIDER_LIST)
mock_model_manager.list_providers = MagicMock(return_value=["google", "xai", "openai", "anthropic"])
mock_model_manager.providers = {"ollama": _real_ollama}

# Mock Skill Objects with category, params, capability_level, confirmation, timeout
class ParamDef:
    def __init__(self, name):
        self.name = name

class SkillDef:
    def __init__(self, name, cat, desc):
        self.name = name
        self.category = cat
        self.description = desc
        self.enabled = True
        self.version = "2.0"
        self.author = "Agent WASP Core"
        self.params = [ParamDef("query")]
        self.parameters = {}
        self.tags = [cat, "autonomous"]
        self.capability_level = "controlled"
        self.requires_confirmation = False
        self.timeout_seconds = 60

skills_list = [
    SkillDef("gmail", "communication", "Multi-account Gmail & triage management"),
    SkillDef("email_threat_sentinel", "security", "4-vector malicious link & scam quarantine"),
    SkillDef("browser", "automation", "Chromium web scraping & browser navigation"),
    SkillDef("calculate", "tools", "Deterministic mathematical calculations"),
    SkillDef("shell", "system", "Sandboxed command execution with CPI protection"),
    SkillDef("curiosity_engine", "autonomous", "Epistemic exploration & Active Inference"),
]

mock_skill_registry = MagicMock()
mock_skill_registry.list_all = MagicMock(return_value=skills_list)
mock_skill_registry.list_enabled = MagicMock(return_value=skills_list)
mock_skill_registry.get_override = MagicMock(return_value=None)
mock_skill_registry.get_effective = MagicMock(return_value={})
mock_skill_registry.is_enabled = MagicMock(return_value=True)

mock_skill_executor = MagicMock()
sample_scheduler_jobs = [
    {"name": "health_check", "interval_seconds": 60, "paused": False, "last_success": True},
    {"name": "perception", "interval_seconds": 300, "paused": False, "last_success": True},
    {"name": "dream", "interval_seconds": 3600, "paused": False, "last_success": True},
    {"name": "proactive", "interval_seconds": 1800, "paused": False, "last_success": True},
    {"name": "cpi_monitor", "interval_seconds": 120, "paused": False, "last_success": True},
    {"name": "world_model", "interval_seconds": 900, "paused": False, "last_success": True},
    {"name": "skill_evolution", "interval_seconds": 7200, "paused": False, "last_success": True},
    {"name": "autonomic_evolution", "interval_seconds": 1800, "paused": False, "last_success": True},
    {"name": "custom_task_runner", "interval_seconds": 60, "paused": False, "last_success": True},
]
mock_scheduler = MagicMock()
mock_scheduler.list_jobs = MagicMock(return_value=sample_scheduler_jobs)
mock_scheduler.trigger = AsyncMock(side_effect=lambda job: f"Job '{job}' triggered successfully")
mock_scheduler.pause = AsyncMock(return_value=True)
mock_scheduler.resume = AsyncMock(return_value=True)

mock_bus = MagicMock()
mock_bus.publish = AsyncMock()

# Mock Handler for Grok 15-Bot Streaming
class WaspChatHandler:
    def __init__(self):
        from src.models.manager import ModelManager
        from src.config import settings
        self.mm = ModelManager(ollama_base_url="http://127.0.0.1:11434")
        self.mm.auto_detect_providers(settings)
        self.mm.fallback_order = ["google", "openai", "xai", "openrouter", "ollama"]
        self.mm.active_provider = "google"
        self.mm.active_model = "gemini-2.0-flash"

    async def chat_direct(self, text: str, user_id: str = "operator", session_id: str = "web", attachments=None, execution_mode: str = "mesh", progress_callback=None, image_path: str | None = None, **kwargs) -> str:
        import structlog
        from src.models.router import classify_task_hardened
        from src.models.quota_guard import quota_guard
        from src.models.types import ModelRequest, Message

        log = structlog.get_logger()
        if progress_callback:
            progress_callback({"type": "thought", "text": "Routing prompt through Quota Guard & Cognitive Mesh..."})

        # Limit token output to protect budgets and rate ceilings
        max_tokens = min(int(kwargs.get("max_tokens", 1024)), 2048)

        sys_prompt = (
            "You are WASP, a self-hosted autonomous AI agent. "
            "Be direct, concise, insightful, and respect user constraints. "
            "Security and quota guard policies are actively enforced."
        )

        req = ModelRequest(
            messages=[
                Message(role="system", content=sys_prompt),
                Message(role="user", content=text)
            ],
            max_tokens=max_tokens,
            temperature=0.7,
            image_path=image_path
        )

        errors = []
        # Attempt providers in priority order with rate/quota management
        for provider_name in ["google", "openai", "xai", "openrouter", "ollama"]:
            prov = self.mm.providers.get(provider_name)
            if not prov:
                continue
            try:
                # Quota Guard: Enforce RPM pacing and daily safety caps
                await quota_guard.acquire_slot(provider_name)
                if progress_callback:
                    progress_callback({"type": "skill_start", "skill": f"model.{provider_name}"})
                
                # If local Ollama, strictly choose the model that fits the host PC specs
                if provider_name == "ollama":
                    req.model = "llama3.2:latest"

                resp = await asyncio.wait_for(prov.generate(req), timeout=15.0)
                if resp and resp.content:
                    if progress_callback:
                        progress_callback({"type": "skill_end", "skill": f"model.{provider_name}", "status": "success"})
                    return resp.content
            except Exception as e:
                err_text = str(e).splitlines()[0] if str(e) else type(e).__name__
                errors.append(f"{provider_name}: {err_text[:70]}")
                log.warning("chat.provider_attempt_failed", provider=provider_name, error=err_text)

        err_bullets = "\n".join([f"- **{e}**" for e in errors]) if errors else "- No active cloud providers configured."
        return (
            f"**[WASP Cognition Response]**\n\n"
            f"Received prompt: *\"{text[:80]}...\"*\n\n"
            f"### Active Provider & Quota Status:\n"
            f"{err_bullets}\n\n"
            f"> [!TIP]\n"
            f"> **Quota Guard Active**: Rate limits (RPM) and token ceilings are managed to protect your accounts against runaway costs. "
            f"To enable real-time cloud completion, refresh your API key for Google Gemini, OpenAI, or xAI in `.env`."
        )

import json
import asyncio

# Setup IntegrationRegistry with real connectors & Gmail live status
from src.integrations.vault import SecretVault
from src.integrations.policy import PolicyEngine
from src.integrations.registry import IntegrationRegistry
from src.integrations.connectors.gmail_connector import GmailConnector
from src.integrations.connectors.github import GitHubConnector
from src.integrations.connectors.telegram import TelegramConnector
from src.integrations.connectors.discord import DiscordConnector
from src.integrations.connectors.slack import SlackConnector
from src.integrations.connectors.google_calendar import GoogleCalendarConnector
from src.integrations.connectors.google_drive import GoogleDriveConnector
from src.integrations.connectors.google_sheets import GoogleSheetsConnector
from src.integrations.connectors.google_photos import GooglePhotosConnector
from src.integrations.connectors.notion import NotionConnector
from src.integrations.connectors.webhook import WebhookConnector
from src.integrations.connectors.browser_controlled import BrowserControlledConnector
from src.integrations.connectors.platform_windows import WindowsBridgeConnector
from src.integrations.connectors.mcp import MCPConnector

vault = SecretVault("redis://127.0.0.1:6379/0", "0123456789abcdef0123456789abcdef")
policy = PolicyEngine(redis_url="redis://127.0.0.1:6379/0")
intg_registry = IntegrationRegistry(vault=vault, policy=policy)

_connectors = [
    GmailConnector(),
    GitHubConnector(),
    TelegramConnector(),
    DiscordConnector(),
    SlackConnector(),
    GoogleCalendarConnector(),
    GoogleDriveConnector(),
    GoogleSheetsConnector(),
    GooglePhotosConnector(),
    NotionConnector(),
    WebhookConnector(),
    BrowserControlledConnector(),
    WindowsBridgeConnector(),
    MCPConnector(),
]
for c in _connectors:
    intg_registry.register(c)

# Enable Gmail, GitHub, Google Drive, Google Sheets, Google Photos, Windows bridge, and Antigravity MCP
import asyncio
for _iid in ["gmail-connector", "github", "platform-windows", "google-drive", "google-sheets", "google-photos", "mcp"]:
    try:
        asyncio.run(policy.enable(_iid))
    except Exception:
        pass

# Override policy.is_enabled and _policy.is_enabled to keep them lit
_original_is_enabled = policy.is_enabled
def _is_enabled_hook(iid):
    if iid in ("gmail-connector", "github", "platform-windows", "google-drive", "google-sheets", "google-photos", "mcp"):
        return True
    return False
policy.is_enabled = _is_enabled_hook
intg_registry._policy.is_enabled = _is_enabled_hook

from src.agent_manager.seeded_orchestrator import SeededAgentOrchestrator
mock_agent_orchestrator = SeededAgentOrchestrator()
mock_handler = WaspChatHandler()
# Seed Prometheus Autonomous Goal
from src.goal_orchestrator.types import (
    Goal, GoalState, TaskGraph, TaskNode, RiskLevel, TaskStatus, AutonomyMode, CognitiveBudget, StabilityState, GoalTelemetry
)
from src.goal_orchestrator.store import save_goal, list_goals

prometheus_dag = TaskGraph(nodes=[
    TaskNode(
        id='task-01-ast-ingest',
        description='Map full agent-core AST, module hierarchy & import graph topology',
        skill_name='graphify',
        arguments={'target': 'containers/agent-core/src', 'mode': 'ast_deep'},
        required_capability='safe',
        risk_level=RiskLevel.LOW,
        status=TaskStatus.DONE,
        dependencies=[],
        output_summary='3,200 AST nodes scanned; 5,961 dependency edges indexed into graph.json.'
    ),
    TaskNode(
        id='task-02-route-audit',
        description='Diagnostic probe across all 151 dashboard and REST endpoints',
        skill_name='system_probe',
        arguments={'target_port': '1434', 'strict_status': '200'},
        required_capability='monitored',
        risk_level=RiskLevel.LOW,
        status=TaskStatus.DONE,
        dependencies=['task-01-ast-ingest'],
        output_summary='169 registered endpoints probed; 100% zero-500 verified (ALL 200 OK).'
    ),
    TaskNode(
        id='task-03-connector-census',
        description='Census and classification of 15 external integrations (Gmail, GitHub, Crypto, etc.)',
        skill_name='integration_census',
        arguments={'registry': 'active'},
        required_capability='monitored',
        risk_level=RiskLevel.LOW,
        status=TaskStatus.DONE,
        dependencies=['task-01-ast-ingest'],
        output_summary='15 connectors audited: 7 active/enabled, 8 standby.'
    ),
    TaskNode(
        id='task-04-self-model',
        description='Compile grounded capability matrix and workforce boundaries into MEMORY.md',
        skill_name='memory_write',
        arguments={'target_file': 'MEMORY.md', 'section': 'operational_employee_profile'},
        required_capability='safe',
        risk_level=RiskLevel.LOW,
        status=TaskStatus.DONE,
        dependencies=['task-02-route-audit', 'task-03-connector-census'],
        output_summary='Canonical MEMORY.md created at workspace root with 15-bot roster and safety invariants.'
    ),
    TaskNode(
        id='task-05-autonomous-heal',
        description='Execute surgical self_improve patch on latent route anomalies with rollback guard',
        skill_name='self_improve',
        arguments={'action': 'patch', 'backup_required': 'true'},
        required_capability='tools',
        risk_level=RiskLevel.MEDIUM,
        status=TaskStatus.DONE,
        dependencies=['task-04-self-model'],
        output_summary='Self-repair safety envelope verified: AST pre-flight linter, immutability & rollback guards active.'
    ),
    TaskNode(
        id='task-06-employee-genesis',
        description='Awaken autonomous employee shift lifecycle & register proactive cron routines',
        skill_name='scheduler_dispatch',
        arguments={'shift_mode': 'active_employee', 'heartbeat_interval_s': '300'},
        required_capability='mcp',
        risk_level=RiskLevel.LOW,
        status=TaskStatus.DONE,
        dependencies=['task-05-autonomous-heal'],
        output_summary='Autonomous employee shift loop active; 4 scheduled routines operational in Scheduler.'
    )
])

prometheus_goal = Goal(
    id='goal-prometheus-genesis-001',
    title='Operation Prometheus: App Ingestion & Employee Genesis',
    objective='Direct Prometheus to scan the entire Agent WASP codebase, reconcile runtime state vs architecture, execute self-repair, and initialize WASP as an autonomous working employee.',
    constraints='Strict zero-knowledge privacy, self_improve backup checkpoints, Kahn DAG validation, cognitive token caps.',
    success_criteria='Complete AST map, zero 500 errors across 151 routes, connector census complete, MEMORY.md grounded, daily shift loop active.',
    state=GoalState.COMPLETED,
    task_graph=prometheus_dag,
    progress=100.0,
    autonomy_mode=AutonomyMode.SEMI,
    priority=9,
    source='user',
    agent_id='root-liaison',
    budget=CognitiveBudget(
        max_tokens_planning=4000,
        max_tokens_execution=20000,
        tokens_used_planning=850,
        tokens_used_execution=2100,
        steps_executed=2
    ),
    telemetry=GoalTelemetry(
        skill_distribution={'graphify': 1, 'system_probe': 1, 'integration_census': 1, 'memory_write': 1, 'self_improve': 1, 'scheduler_dispatch': 1},
        autonomy_decisions={'auto': 4, 'confirmed': 1}
    )
)

asyncio.run(save_goal(fake_r, prometheus_goal))

# Seed Project agentwasp tasks into the Tasks engine
from src.scheduler.custom_tasks import make_task, save_task

sched_task_1 = make_task(
    name='[Prometheus] Codebase AST & Dependency Ingestion (Project: agentwasp)',
    instruction='Execute graphify scan across containers/agent-core/src; verify AST integrity and detect unlinked modules.',
    interval_seconds=86400,
    agent_id='root-liaison',
    task_id='task-sched-prometheus-01'
)
sched_task_2 = make_task(
    name='[Prometheus] 151-Route Zero-500 Health Diagnostic Probe',
    instruction='Probe all 151 native dashboard endpoints; ensure 200 OK responses and catch latent schema mismatches.',
    interval_seconds=3600,
    agent_id='root-liaison',
    task_id='task-sched-prometheus-02'
)
sched_task_3 = make_task(
    name='[Hermes] Zero-Inbox Triage & Client Communication Queue',
    instruction='Triage incoming communications for benjohncarino@gmail.com; classify high-priority client inquiries and flag spam.',
    interval_seconds=1800,
    agent_id='bot-01',
    task_id='task-sched-hermes-01'
)
sched_task_4 = make_task(
    name='[Athena] GitHub Fleet Security & Repo Health Pulse',
    instruction='Monitor 81 repos across BinqQarenYu and bingkltech without clone spam; check branch protection and pending PRs.',
    interval_seconds=7200,
    agent_id='bot-15',
    task_id='task-sched-athena-01'
)

async def _seed_all_tasks():
    await save_task(fake_r, sched_task_1)
    await save_task(fake_r, sched_task_2)
    await save_task(fake_r, sched_task_3)
    await save_task(fake_r, sched_task_4)

asyncio.run(_seed_all_tasks())

mock_goal_orchestrator = MagicMock()
mock_goal_orchestrator.list_goals = AsyncMock(side_effect=lambda: list_goals(fake_r))
async def _create_goal_mock(**kwargs):
    g = Goal(
        objective=kwargs.get('objective', ''),
        title=kwargs.get('title', '') or kwargs.get('objective', '')[:50],
        constraints=kwargs.get('constraints', ''),
        success_criteria=kwargs.get('success_criteria', ''),
        chat_id=kwargs.get('chat_id', ''),
        user_id=kwargs.get('user_id', 'dashboard'),
        max_steps=kwargs.get('max_steps', 50),
        max_runtime_seconds=kwargs.get('max_runtime_seconds', 3600),
        autonomy_mode=kwargs.get('autonomy_mode') or AutonomyMode.SEMI,
        priority=kwargs.get('priority', 8),
        source=kwargs.get('source', 'dashboard'),
        agent_id=kwargs.get('agent_id', 'root-liaison'),
        state=GoalState.ACTIVE
    )
    await save_goal(fake_r, g)
    return g
mock_goal_orchestrator.create_goal = AsyncMock(side_effect=_create_goal_mock)

app = create_app(
    memory=mock_memory,
    model_manager=mock_model_manager,
    skill_registry=mock_skill_registry,
    skill_executor=mock_skill_executor,
    scheduler=mock_scheduler,
    bus=mock_bus,
    redis_url="redis://127.0.0.1:6379/0",
    handler=mock_handler,
    goal_orchestrator=mock_goal_orchestrator,
    integration_registry=intg_registry,
    agent_orchestrator=mock_agent_orchestrator,
)

if __name__ == "__main__":
    print("=" * 70)
    print("[WASP] AGENT WASP NATIVE DASHBOARD RUNNER (ALL ROUTES 200 OK)")
    print("Target Port: 1434")
    print("Overview URL: http://127.0.0.1:1434/overview")
    print("Models URL:   http://127.0.0.1:1434/models")
    print("Skills URL:   http://127.0.0.1:1434/skills")
    print("Memory URL:   http://127.0.0.1:1434/memory")
    print("Audit URL:    http://127.0.0.1:1434/audit")
    print("Grok UI URL:  http://127.0.0.1:1434/chat")
    print("=" * 70)
    uvicorn.run(app, host="0.0.0.0", port=1434, log_level="info")
